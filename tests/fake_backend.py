"""Test double for the agent backend contract: no process, no network, deterministic output."""
import time


class FakeBackend:
    """Modes: normal | slow | fail_start | fail_send | fail_stop."""

    def __init__(self,mode='normal',reply=('你','好'),delay=0.0,remote_session_id='fake-remote-1'):
        self.mode=mode;self.reply=list(reply);self.delay=delay
        self.remote_session_id=remote_session_id
        self.started=False;self.stop_calls=0;self.sent=[]
        self.on_delta=None
        self._sink=None

    def bind(self,emit):self._sink=emit

    def start(self):
        if self.mode=='fail_start':raise RuntimeError('fake start failure')
        self.started=True

    def send(self,text):
        if self.mode=='fail_send':raise RuntimeError('fake send failure')
        self.sent.append(text)
        if self.mode=='noisy':
            self._sink({'type':'weird_private_event','x':1})   # outside the documented contract
            self._sink('not-an-event')                          # not even an object
        for chunk in self.reply:
            if self.delay:time.sleep(self.delay)
            self._sink({'type':'assistant_text_delta','text':chunk})
            if self.on_delta:self.on_delta(chunk)
        self._sink({'type':'assistant_message_completed','text_chars':sum(len(chunk) for chunk in self.reply)})

    def stop(self):
        if self.mode=='fail_stop':raise RuntimeError('fake stop failure')
        self.stop_calls+=1

    def status(self):
        return {'started':self.started,'sent':len(self.sent),'remote_session_id':self.remote_session_id}
