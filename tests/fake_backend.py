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
        self.stop_entered=threading.Event()
        self.stop_release=threading.Event()
        self.on_delta=None
        self._sink=None
        self.interactions={}

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
        if self.mode=='ask_interaction':
            # Offers one pending question, so a bridge test can answer it without Reasonix.
            self.interactions['fake-interaction-1']={'kind':'question','option_ids':{'a','b'}}
            self._sink({'type':'question_requested','interaction_id':'fake-interaction-1',
                        'question':'pick one','multi':False,
                        'options':[{'option_id':'a','label':'A','description':''},
                                   {'option_id':'b','label':'B','description':''}]})
            return
        if self.mode=='ask_question':
            # A short reply, then an interaction request that must not overtake it in the log.
            self._sink({'type':'assistant_text_delta','text':'半'})
            self._sink({'type':'question_requested','interaction_id':'fake-interaction-1',
                        'question':'pick one','multi':False,
                        'options':[{'option_id':'q1:1','label':'A','description':''}]})
            return
        if self.mode=='cancelled_turn':
            # Mirrors the adapter's contract: the turn's events are delivered, its outcome is
            # reported, and only then does the turn fail as cancelled.
            self._sink({'type':'assistant_text_delta','text':'半'})
            for kind in self.tool_events:self._sink({'type':kind})
            self._sink({'type':'turn_settled','stop_reason':'cancelled'})
            raise CancelledError('fake turn cancelled')
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
            self._sink({'type':'turn_settled','stop_reason':'end_turn'})
            return
        for chunk in self.reply:
            if self.delay:time.sleep(self.delay)
            self._sink({'type':'assistant_text_delta','text':chunk})
            if self.on_delta:self.on_delta(chunk)
        self._sink({'type':'turn_settled','stop_reason':'end_turn'})

    def cancel(self):
        """The double only ever cancels a turn it is running, so this always reports a request."""
        self.cancel_calls+=1;self.cancelled=True;self.release.set()
        return True

    def answer_question(self,interaction_id,option_id):
        return self._settle(interaction_id,'question',option_id,'question_settled')

    def cancel_question(self,interaction_id):
        return self._settle(interaction_id,'question',None,'question_settled')

    def answer_permission(self,interaction_id,option_id):
        return self._settle(interaction_id,'permission',option_id,'permission_settled')

    def cancel_permission(self,interaction_id):
        return self._settle(interaction_id,'permission',None,'permission_settled')

    def _settle(self,interaction_id,kind,option_id,event_type):
        """Mirrors the real backend's contract: unknown, wrong kind, twice and bad options fail."""
        request=self.interactions.get(interaction_id)
        if request is None:raise ValueError('该交互请求不存在或已结束：'+str(interaction_id))
        if request['kind']!=kind:raise ValueError('该交互请求的类型不匹配：'+str(interaction_id))
        if option_id is not None and option_id not in request['option_ids']:
            raise ValueError('所选答案不属于该交互请求：'+str(option_id))
        self.interactions.pop(interaction_id,None)
        event={'type':event_type,'interaction_id':interaction_id,
               'outcome':'selected' if option_id else 'cancelled'}
        if option_id:event['option_id']=option_id
        self._sink(event)
        return True

    def stop(self):
        if self.mode=='fail_stop':raise RuntimeError('fake stop failure')
        if self.mode=='hang_stop':
            # A close that never returns, so a caller can prove it still reached the sessions behind
            # this one. The wait is released by the test so no thread is left behind.
            self.stop_calls+=1;self.stop_entered.set();self.stop_release.wait(30)
            return
        self.stop_calls+=1

    def status(self):
        return {'started':self.started,'sent':len(self.sent),'remote_session_id':self.remote_session_id}
