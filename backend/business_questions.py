"""Persistent business answers, independent of ACP's single-option permission wire."""
import json
from sqlalchemy import select
from .db import Session, WorkbenchMeta, ChatQuestion, Message, require, obj, uid, now
from . import draft


def ask(did,prompt,options=None,multiple=False):
    options=options or []
    if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=1000:raise ValueError('问题需为1–1000字')
    if not isinstance(options,list) or len(options)>8 or any(not isinstance(x,str) or not x.strip() or len(x)>200 for x in options):raise ValueError('最多8个文字选项，每项1–200字')
    with draft.WRITE_LOCK,Session.begin() as s:
        active=require(s,WorkbenchMeta,'active-chat:'+did)
        turn=active.data['turn_id'];mid=active.data['message_id']
        message=s.get(Message,mid)
        if not message or message.status!='running':
            return {'status':'interrupted','approval':False,'message':'本轮已结束，问题未提出；请在正文中直接说明缺失信息。'}
        questions=list(s.scalars(select(ChatQuestion).where(ChatQuestion.dataset_id==did,ChatQuestion.turn_id==turn)))
        if len(questions)>=3:return {'status':'limit','approval':False,'message':'本轮已经提出三个问题，请根据已有回答继续或用正文说明缺失信息。'}
        if any(q.status=='pending' for q in questions):raise ValueError('请先等待当前问题的回答')
        q=ChatQuestion(id=uid(),dataset_id=did,turn_id=turn,message_id=mid,prompt=prompt.strip(),options=[{'option_id':str(i+1),'label':v.strip()} for i,v in enumerate(options)],status='pending')
        s.add(q);s.flush()
        s.add(WorkbenchMeta(id='business-question:'+q.id,dataset_id=did,kind='business_question',data={'multiple':bool(multiple),'answer':None}))
        result={**obj(q),'business':True,'multiple':bool(multiple)};draft.emit(s,did,'question',result)
        labels=[x['label'] for x in result['options']]
    # The answer arrives through the persisted web card and resumes the conversation as a
    # new turn. Waiting for it inside the MCP call is what caused the 11-minute silence.
    return {'status':'pending','question_id':result['id'],'prompt':prompt.strip(),'options':labels,'multiple':bool(multiple),'approval':False,
        'instruction':'问题已显示在工作台；用户回答后系统会自动从下一步继续。不要重复提问，也不要使用原生 ask 再问同一问题。'}


def _answer_sentence(question,payload):
    parts=[]
    if payload['labels']:parts.append('选择了：'+'、'.join(payload['labels']))
    if payload['text']:parts.append('补充说明：'+payload['text'])
    if payload['skipped']:parts.append('跳过此问题，请按你的判断继续')
    return '针对上一轮的问题“'+str(question.prompt or '')[:200]+'”，'+('；'.join(parts) or '不作回答')+'。'


def _queue_followup(s,did,question,payload):
    """Return the follow-up key to resume, or None when one is already queued."""
    key='chat-followup:'+did;text=_answer_sentence(question,payload)
    existing=s.get(WorkbenchMeta,key)
    if existing:
        data=dict(existing.data);data['text']=(str(data.get('text') or '')+'\n'+text)[:20000];existing.data=data;return None
    active=s.get(WorkbenchMeta,'active-chat:'+did)
    cid=(active.data.get('conversation_id') if active else None)
    if not cid:
        from .chat_threads import ensure
        cid=ensure(s,did)
    thread=s.get(WorkbenchMeta,cid);data=dict(thread.data or {}) if thread else {}
    payload_out={'text':text,'conversation_id':cid}
    for field in ('model','vision_model','text_credential_id','vision_credential_id'):
        if data.get(field):payload_out[field]=data[field]
    s.add(WorkbenchMeta(id=key,dataset_id=did,kind='chat_followup',data=payload_out))
    return key


def answer(did,qid,option_ids=None,text='',skip=False):
    option_ids=option_ids or []
    if not isinstance(option_ids,list) or any(not isinstance(x,str) for x in option_ids):raise ValueError('选项格式无效')
    if not isinstance(text,str) or len(text)>20000:raise ValueError('回答最多20000字')
    with draft.WRITE_LOCK,Session.begin() as s:
        q=require(s,ChatQuestion,qid);meta=require(s,WorkbenchMeta,'business-question:'+qid)
        if q.dataset_id!=did:raise ValueError('问题不属于当前表格')
        selected=sorted(set(option_ids));offered={x['option_id']:x['label'] for x in q.options}
        if not set(selected)<=set(offered):raise ValueError('选项不属于当前问题')
        if len(selected)>1 and not meta.data['multiple']:raise ValueError('当前问题为单选')
        if skip and (selected or text.strip()):raise ValueError('跳过问题不能同时提交答案')
        if not skip and not selected and not text.strip():raise ValueError('请选择或填写回答')
        payload={'option_ids':selected,'labels':[offered[x] for x in selected],'text':text.strip(),'skipped':bool(skip)}
        if meta.data.get('answer')==payload:return {**obj(q),'business':True,'multiple':meta.data['multiple']}
        if q.status!='pending':raise ValueError('问题已结束，不能更改答案')
        meta.data={**meta.data,'answer':payload};q.status='skipped' if skip else 'answered';q.answered_at=now()
        q.selected_label='；'.join(payload['labels']+[text.strip()]).strip('；')
        q.selected_option_id=json.dumps(selected)
        result={**obj(q),'business':True,'multiple':meta.data['multiple']};draft.emit(s,did,'question',result)
        # A business question outlives the turn that asked it: the answer becomes the next
        # turn instead of depending on a still-open ACP request.
        queued=_queue_followup(s,did,q,payload)
    if queued:
        from . import conversation
        conversation.threading.Thread(target=conversation._resume_queued,args=(did,queued),daemon=True).start()
    return result
