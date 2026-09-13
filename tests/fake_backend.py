"""Test double for the agent backend contract: no process, no network, deterministic output."""
import threading
import time
from backend.agent.backend import CancelledError,RuntimeUnavailable,TurnError


class FakeBackend:
    """Modes: normal | slow | turn_fail | fatal_fail | crash_send | fail_start | fail_stop | noisy.

    slow blocks inside send() until cancel() releases it, so cancellation is tested by handshake
    rather than by sleeping and hoping the timing worked out.
    """

    def __init__(self,mode='normal',reply=('你','好'),delay=0.0,remote_session_id='fake-remote-1',tool_events=()):
        self.mode=mode;self.reply=list(reply);self.delay=delay
        self.tool_events=list(tool_events)
        self.remote_session_id=remote_session_id
        self.started=False;self.stop_calls=0;self.cancel_calls=0;self.sent=[]
        self.cancelled=False
        self.entered=threading.Event()
        self.release=threading.Event()
        self.on_delta=None
        self._sink=None

    def bind(self,emit):self._sink=emit

    def start(self):
        if self.mode=='fail_start':raise RuntimeError('fake start failure')
        self.started=True

    def send(self,text):
        if self.mode=='turn_fail':raise TurnError('fake turn failure')
        if self.mode=='fatal_fail':raise RuntimeUnavailable('fake runtime unavailable')
        if self.mode=='crash_send':raise RuntimeError('fake contract violation')
        self.sent.append(text)
        self.entered.clear();self.release.clear();self.cancelled=False   # fresh turn state
        for kind in self.tool_events:self._sink({'type':kind})
        if self.mode=='noisy':
            self._sink({'type':'weird_private_event','x':1})   # outside the documented contract
            self._sink('not-an-event')                          # not even an object
        if self.mode=='mid_fail':
            self._sink({'type':'assistant_text_delta','text':'半'})
            raise TurnError('fake turn failed after output')
        if self.mode=='slow':
            self._sink({'type':'assistant_text_delta','text':'半'})   # buffered, then cancelled
            self.entered.set()
            self.release.wait(timeout=5)
            if self.cancelled:raise CancelledError('fake turn cancelled')
            self._sink({'type':'assistant_message_completed','text_chars':1})
            return
        for chunk in self.reply:
            if self.delay:time.sleep(self.delay)
            self._sink({'type':'assistant_text_delta','text':chunk})
            if self.on_delta:self.on_delta(chunk)
        self._sink({'type':'assistant_message_completed','text_chars':sum(len(chunk) for chunk in self.reply)})

    def cancel(self):
        self.cancel_calls+=1;self.cancelled=True;self.release.set()

    def stop(self):
        if self.mode=='fail_stop':raise RuntimeError('fake stop failure')
        self.stop_calls+=1

    def status(self):
        return {'started':self.started,'sent':len(self.sent),'remote_session_id':self.remote_session_id}
