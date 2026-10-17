"""Reasonix CLI process runtime: spawn, stdio transport, JSON-RPC, timeouts, observability.

Knows nothing about datasets, drafts, tags, proposals, messages or conversations. It receives
an executable, args, cwd, env and callbacks, and reports structured protocol messages,
diagnostics and lifecycle events. Exception types and messages match the previous transport so
callers that branch on them keep working.

The peer speaks UTF-8, newline-delimited JSON. Decoding is explicit so a Windows console code
page can never decide how ACP frames are read, and universal newlines keep CRLF peers working.
"""
import concurrent.futures
import os
import subprocess
import threading
import time
from datetime import datetime,timezone
from . import protocol

DEFAULT_REQUEST_TIMEOUT=900
TERMINATE_TIMEOUT=8
READER_JOIN_TIMEOUT=5
STDERR_RING_CAPACITY=200
PROTOCOL_RING_CAPACITY=50
LIFECYCLE_RING_CAPACITY=200
DETAIL_LIMIT=200


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class ACPRemoteError(RuntimeError):
    """A JSON-RPC error reply with the peer's machine-readable code retained."""
    def __init__(self,error):
        error=error if isinstance(error,dict) else {}
        self.error_code=error.get('code')
        self.remote_message=str(error.get('message') or 'ACP error')
        super().__init__(self.remote_message)


class RingBuffer:
    """Fixed-capacity tail: diagnostics are readable long after the moment they happened."""
    def __init__(self,capacity):
        self.capacity=max(1,int(capacity));self.items=[]
    def add(self,item):
        self.items.append(item)
        if len(self.items)>self.capacity:del self.items[:-self.capacity]
        return item
    def tail(self,count=None):
        return list(self.items if count is None else self.items[-max(0,int(count)):])
    def __len__(self):return len(self.items)


# --- process ownership ----------------------------------------------------
#
# This layer owns the OS lifecycle of the tree it spawns. The peer is launched with its stdio bound
# to this process, so it commonly notices the parent's death by itself when reading stdin returns
# EOF — but that is the peer's behaviour, not a guarantee made here. On Windows an anonymous job
# object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE turns it into one: the handle lives in this
# process, and when the last handle closes — a normal stop, a crashed or hard-killed workbench, a
# logoff — the OS terminates every process still associated with the job. Membership is inherited by
# descendants created after association, so the peer's own children are covered without this code
# ever naming them.
#
# One anonymous job per runtime: it can only ever contain processes this runtime spawned, so a
# manual Reasonix, another workbench or another project's instance is out of reach by construction.
# Nothing here enumerates or kills by image name.

if os.name=='nt':
    import ctypes
    from ctypes import wintypes

    _KERNEL32=ctypes.WinDLL('kernel32',use_last_error=True)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION=9
    _PROCESS_SET_QUOTA=0x0100
    _PROCESS_TERMINATE=0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_=[('ReadOperationCount',ctypes.c_ulonglong),('WriteOperationCount',ctypes.c_ulonglong),
                  ('OtherOperationCount',ctypes.c_ulonglong),('ReadTransferCount',ctypes.c_ulonglong),
                  ('WriteTransferCount',ctypes.c_ulonglong),('OtherTransferCount',ctypes.c_ulonglong)]

    class _JOB_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                  ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
                  ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
                  ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),
                  ('SchedulingClass',wintypes.DWORD)]

    class _JOB_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_=[('BasicLimitInformation',_JOB_BASIC_LIMIT_INFORMATION),('IoInfo',_IO_COUNTERS),
                  ('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                  ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]


class ProcessJob:
    """One anonymous job object owned by one runtime: the OS-level owner of its process tree.

    Every failure is reported and never raised. Owning the peer's lifecycle is hardening; a host
    whose job policy refuses association must not be the reason the workbench cannot run an agent,
    so the runtime keeps running on stdio alone and says in diagnostics why it is unprotected.
    """

    def __init__(self):
        self.supported=os.name=='nt'
        self.handle=None
        self.created=False
        self.assign_attempted=False
        self.assigned=False
        self.error=None

    def _fail(self,message):
        self.error=str(message)[:DETAIL_LIMIT]
        return False

    def create(self):
        """Create the job and require it to terminate its members when the last handle closes."""
        if not self.supported:return False
        handle=_KERNEL32.CreateJobObjectW(None,None)
        if not handle:return self._fail('CreateJobObjectW failed (%d)'%ctypes.get_last_error())
        limits=_JOB_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags=_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _KERNEL32.SetInformationJobObject(handle,_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                 ctypes.byref(limits),ctypes.sizeof(limits)):
            code=ctypes.get_last_error();_KERNEL32.CloseHandle(handle)
            return self._fail('SetInformationJobObject failed (%d)'%code)
        self.handle=handle;self.created=True
        return True

    def assign(self,pid):
        """Associate one process with the job. Called as early as the spawn allows, before any I/O.

        An outer job on this host can still make the assignment fail (nested membership depends on
        the hierarchy the host established), which is exactly why the return value is read and the
        error kept instead of being assumed.
        """
        if not self.supported or self.handle is None:return False
        self.assign_attempted=True
        process=_KERNEL32.OpenProcess(_PROCESS_SET_QUOTA|_PROCESS_TERMINATE,False,int(pid))
        if not process:return self._fail('OpenProcess failed (%d)'%ctypes.get_last_error())
        try:
            if not _KERNEL32.AssignProcessToJobObject(self.handle,process):
                return self._fail('AssignProcessToJobObject failed (%d)'%ctypes.get_last_error())
        finally:_KERNEL32.CloseHandle(process)
        self.assigned=True
        return True

    def release(self):
        """Close the handle. Idempotent: closing the last one is what ends any remaining member."""
        if self.handle is None:return
        handle,self.handle=self.handle,None
        _KERNEL32.CloseHandle(handle)

    def info(self):
        """Narrow observability. No handle, no pid, and nothing inferred beyond what was observed."""
        return {'supported':self.supported,'created':self.created,'assigned':self.assigned,'error':self.error}


class Runtime:
    def __init__(self,executable,args,cwd=None,env=None,on_event=None,on_diagnostic=None,on_lifecycle=None):
        self.executable=executable;self.args=list(args);self.cwd=cwd;self.env=env
        self.on_event=on_event or (lambda message:None)
        self.on_diagnostic=on_diagnostic or (lambda kind,line:None)
        self.on_lifecycle=on_lifecycle or (lambda event:None)
        self.pending={};self.counter=0;self.lock=threading.Lock()
        self.closed=False;self.proc=None;self._reader=None;self._stderr_reader=None
        self._job=None
        self.started_at=None;self.stopped_at=None;self.last_activity_at=None
        self._exit_noted=False
        self._stderr_ring=RingBuffer(STDERR_RING_CAPACITY)
        self._protocol_ring=RingBuffer(PROTOCOL_RING_CAPACITY)
        self._lifecycle_ring=RingBuffer(LIFECYCLE_RING_CAPACITY)

    # --- read-only status -------------------------------------------------

    @property
    def pid(self):return self.proc.pid if self.proc else None

    @property
    def alive(self):return bool(self.proc) and self.proc.poll() is None

    @property
    def returncode(self):
        """The real exit code once the process has exited, otherwise None."""
        return self.proc.poll() if self.proc else None

    @property
    def reader_alive(self):return bool(self._reader) and self._reader.is_alive()

    # --- lifecycle events -------------------------------------------------

    def _emit(self,type,**detail):
        event={'type':type,'timestamp':timestamp()}
        if self.pid is not None:event['pid']=self.pid
        event.update({key:value for key,value in detail.items() if value is not None})
        self._lifecycle_ring.add(event);self.on_lifecycle(event)
        return event

    def _note_exit(self):
        if self._exit_noted or self.alive:return None
        self._exit_noted=True
        if not self.stopped_at:self.stopped_at=timestamp()
        return self._emit('process_exited',returncode=self.returncode)

    def _wait_reaped(self,timeout=2.0):
        """EOF on stdout means the peer is closing. Give the OS a moment to report the exit
        code, otherwise the exit event would race the process object and be lost."""
        deadline=time.monotonic()+max(0,timeout)
        while self.alive and time.monotonic()<deadline:time.sleep(.02)

    # --- process lifecycle ------------------------------------------------

    def start(self):
        self._emit('process_starting')
        # Own the child's OS lifecycle before it exists, so association can happen the moment the
        # spawn returns. ACP stdio is UTF-8, newline-delimited JSON. text=True gives universal
        # newlines so a CRLF peer keeps working, and encoding is pinned here instead of being
        # inherited from the Windows console code page.
        self._job=ProcessJob();self._job.create()
        try:
            self.proc=subprocess.Popen([self.executable,*self.args],cwd=self.cwd,env=self.env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding='utf-8',errors='replace',bufsize=1,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except BaseException:
            self._job.release()
            raise
        # Nothing is written to the peer before this attempt: it is the earliest point the OS lets
        # us claim the child, and association is inherited by whatever the peer spawns afterwards.
        self._job.assign(self.proc.pid)
        if self._job.supported and not self._job.assigned:
            # Hardening is unavailable on this host, not the session: say why it is unprotected and
            # keep running on stdio alone rather than failing an agent that would otherwise work.
            self._emit('job_unavailable',detail=self._job.error)
            self._job.release()
        self.started_at=timestamp();self.last_activity_at=self.started_at
        self._reader=threading.Thread(target=self._read_stdout,name='reasonix-acp-stdout',daemon=True)
        self._reader.start()
        self._stderr_reader=threading.Thread(target=self._read_stderr,name='reasonix-acp-stderr',daemon=True)
        self._stderr_reader.start()
        self._emit('process_started')
        return self.proc

    def stop(self,terminate_timeout=TERMINATE_TIMEOUT):
        """Graceful terminate, then force kill, then close the streams. Idempotent."""
        if self.closed:return
        self._emit('process_stopping')
        self.closed=True
        if self.alive:
            self.proc.terminate()
            try:self.proc.wait(timeout=terminate_timeout)
            except subprocess.TimeoutExpired:self.kill()
        self._note_exit()
        self._close_streams()
        if not self.stopped_at:self.stopped_at=timestamp()
        self._emit('process_stopped',returncode=self.returncode)
        # Last, and only after the teardown above has run: closing the last job handle is what makes
        # the OS end anything of this runtime's tree that outlived it.
        if self._job:self._job.release()

    def kill(self):
        if self.alive:self.proc.kill()
        self._emit('process_killed',returncode=self.proc.poll() if self.proc else None)

    def join_reader(self,timeout=READER_JOIN_TIMEOUT):
        """Wait for both reader threads to finish so shutdown leaves none behind."""
        deadline=time.monotonic()+max(0,timeout)
        for reader in (self._reader,self._stderr_reader):
            if reader:reader.join(max(0,deadline-time.monotonic()))
        return not self.reader_alive

    @property
    def reader_alive(self):return bool(self._reader) and self._reader.is_alive()

    def _close_streams(self):
        for stream in (self.proc.stdin,self.proc.stdout,self.proc.stderr):
            try:stream.close()
            except (OSError,ValueError):pass

    # --- transport --------------------------------------------------------

    def send(self,message):
        with self.lock:
            if not self.alive:raise RuntimeError('Reasonix 进程已退出')
            self.proc.stdin.write(protocol.line(message));self.proc.stdin.flush()

    def respond(self,ident,result):self.send(protocol.response(ident,result))

    def notify(self,method,params):self.send(protocol.notification(method,params))

    def request(self,method,params,timeout=DEFAULT_REQUEST_TIMEOUT):
        with self.lock:
            self.counter+=1;ident=self.counter
            future=concurrent.futures.Future();self.pending[ident]=future
        started=time.perf_counter()
        self._emit('request_started',ident=ident,method=method,timeout=timeout)
        try:
            self.send(protocol.request(ident,method,params))
            result=future.result(timeout=timeout)
            self._emit('request_completed',ident=ident,method=method,seconds=round(time.perf_counter()-started,3))
            return result
        except concurrent.futures.TimeoutError:
            self._emit('request_timeout',ident=ident,method=method,timeout=timeout,seconds=round(time.perf_counter()-started,3))
            raise RuntimeError(f'Reasonix {method} 超时，可重试未完成项')
        except Exception as error:
            self._emit('request_failed',ident=ident,method=method,detail=f'{type(error).__name__}: {error}'[:DETAIL_LIMIT])
            raise
        finally:self.pending.pop(ident,None)

    # --- diagnostics ------------------------------------------------------

    def diagnostics(self):
        """Recent structured diagnostics: stderr lines, protocol errors and lifecycle events."""
        return {'stderr':self._stderr_ring.tail(),'protocol':self._protocol_ring.tail(),
                'lifecycle':self._lifecycle_ring.tail(),'job':self._job.info() if self._job else None}

    def protocol_errors(self):return self._protocol_ring.tail()

    def lifecycle_tail(self,count=None):return self._lifecycle_ring.tail(count)

    def _note_protocol_error(self,error):
        """One bad frame must not end the session: record it and keep reading."""
        entry=self._protocol_ring.add({'kind':error.kind,'raw':error.raw,'timestamp':timestamp()})
        self._emit('protocol_error',kind=error.kind,raw=error.raw)
        self.on_diagnostic('stdout',error.raw)
        return entry

    def _read_stdout(self):
        try:
            for raw in self.proc.stdout:
                self.last_activity_at=timestamp()
                try:message=protocol.parse_line(raw)
                except protocol.ProtocolError as error:
                    self._note_protocol_error(error);continue
                if protocol.has_method(message):self.on_event(message)
                elif protocol.is_response(message):self._resolve(message)
        except (ValueError,OSError) as error:
            # Closing the pipes during stop()/kill() is expected, not a failure.
            self._emit('reader_closed',detail=type(error).__name__)
        except Exception as error:
            # A real reader failure is recorded rather than lost to a thread traceback.
            self._emit('reader_failed',detail=f'{type(error).__name__}: {error}'[:DETAIL_LIMIT])
        finally:
            self._wait_reaped()
            self._note_exit()
            self._emit('reader_stopped')
            for future in list(self.pending.values()):
                if not future.done():future.set_exception(RuntimeError('Reasonix 连接已断开'))

    def _resolve(self,message):
        future=self.pending.get(protocol.message_id(message))
        if not future or future.done():return
        if protocol.is_error(message):future.set_exception(ACPRemoteError(message.get('error')))
        else:future.set_result(protocol.result_of(message))

    def _read_stderr(self):
        try:
            for raw in self.proc.stderr:
                self.last_activity_at=timestamp()
                self._stderr_ring.add({'line':raw,'timestamp':timestamp()})
                self.on_diagnostic('stderr',raw)
        except (ValueError,OSError) as error:
            self._emit('stderr_closed',detail=type(error).__name__)
        except Exception as error:
            self._emit('stderr_failed',detail=f'{type(error).__name__}: {error}'[:DETAIL_LIMIT])
