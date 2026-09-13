"""Reasonix CLI process runtime: spawn, stdio transport, JSON-RPC, timeouts, lifecycle.

Knows nothing about datasets, drafts, tags, proposals, messages or conversations. It receives
an executable, args, cwd, env and callbacks, and reports protocol messages, raw diagnostics,
lifecycle transitions and transport exceptions. Exception types and messages match the previous
transport so callers that branch on them keep working.
"""
import concurrent.futures
import subprocess
import threading
from . import protocol

DEFAULT_RPC_TIMEOUT=900
TERMINATE_TIMEOUT=8
PROTOCOL_ERROR_LIMIT=20


class Runtime:
    def __init__(self,executable,args,cwd=None,env=None,on_event=None,on_diagnostic=None,on_lifecycle=None):
        self.executable=executable;self.args=list(args);self.cwd=cwd;self.env=env
        self.on_event=on_event or (lambda message:None)
        self.on_diagnostic=on_diagnostic or (lambda kind,line:None)
        self.on_lifecycle=on_lifecycle or (lambda kind:None)
        self.pending={};self.counter=0;self.lock=threading.Lock()
        self.closed=False;self.proc=None;self._protocol_errors=[]

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        self.proc=subprocess.Popen([self.executable,*self.args],cwd=self.cwd,env=self.env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8',errors='replace',bufsize=1,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        threading.Thread(target=self._read_stdout,daemon=True).start()
        threading.Thread(target=self._read_stderr,daemon=True).start()
        self.on_lifecycle('started')
        return self.proc

    @property
    def alive(self):
        return bool(self.proc) and self.proc.poll() is None

    def stop(self,terminate_timeout=TERMINATE_TIMEOUT):
        """Graceful terminate, then force kill, then close the streams. Idempotent."""
        if self.closed:return
        self.closed=True
        if self.alive:
            self.proc.terminate()
            try:self.proc.wait(timeout=terminate_timeout)
            except subprocess.TimeoutExpired:self.kill()
        for stream in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
            try:stream.close()
            except (OSError,ValueError):pass
        self.on_lifecycle('stopped')

    def kill(self):
        if self.alive:self.proc.kill()
        self.on_lifecycle('killed')

    # --- transport ---------------------------------------------------------

    def send(self,message):
        with self.lock:
            if not self.alive:raise RuntimeError('Reasonix 进程已退出')
            self.proc.stdin.write(protocol.line(message));self.proc.stdin.flush()

    def respond(self,ident,result):self.send(protocol.response(ident,result))

    def notify(self,method,params):self.send(protocol.notification(method,params))

    def request(self,method,params,timeout=DEFAULT_RPC_TIMEOUT):
        with self.lock:
            self.counter+=1;ident=self.counter
            future=concurrent.futures.Future();self.pending[ident]=future
        self.send(protocol.request(ident,method,params))
        try:return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:raise RuntimeError(f'Reasonix {method} 超时，可重试未完成项')
        finally:self.pending.pop(ident,None)

    # --- diagnostics -------------------------------------------------------

    def protocol_errors(self):
        """Parse failures seen on stdout; the caller decides whether to surface them."""
        return list(self._protocol_errors)

    def _note_protocol_error(self,error):
        self._protocol_errors.append({'kind':error.kind,'raw':error.raw})
        self._protocol_errors=self._protocol_errors[-PROTOCOL_ERROR_LIMIT:]
        self.on_diagnostic('stdout',error.raw)

    def _read_stdout(self):
        try:
            for raw in self.proc.stdout:
                try:message=protocol.parse_line(raw)
                except protocol.ProtocolError as error:
                    self._note_protocol_error(error);continue
                if protocol.has_method(message):self.on_event(message)
                elif protocol.is_response(message):self._resolve(message)
        finally:
            self.on_lifecycle('reader_stopped')
            for future in list(self.pending.values()):
                if not future.done():future.set_exception(RuntimeError('Reasonix 连接已断开'))

    def _resolve(self,message):
        future=self.pending.get(protocol.message_id(message))
        if not future or future.done():return
        if protocol.is_error(message):future.set_exception(RuntimeError(protocol.error_message(message)))
        else:future.set_result(protocol.result_of(message))

    def _read_stderr(self):
        for raw in self.proc.stderr:
            self.on_diagnostic('stderr',raw)
