"""Persistent business answers, independent of ACP's single-option permission wire."""
import json
import time
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
        questions=list(s.scalars(select(ChatQuestion).where(ChatQuestion.dataset_id==did,ChatQuestion.turn_id==turn)))
        if len(questions)>=3:return {'status':'limit','message':'本轮已经提出三个问题，请根据已有回答继续或用正文说明缺失信息。'}
        if any(q.status=='pending' for q in questions):raise ValueError('请先等待当前问题的回答')
        q=ChatQuestion(id=uid(),dataset_id=did,turn_id=turn,message_id=mid,prompt=prompt.strip(),options=[{'option_id':str(i+1),'label':v.strip()} for i,v in enumerate(options)],status='pending')
        s.add(q);s.flush()
        s.add(WorkbenchMeta(id='business-question:'+q.id,dataset_id=did,kind='business_question',data={'multiple':bool(multiple),'answer':None}))
        result={**obj(q),'business':True,'multiple':bool(multiple)};draft.emit(s,did,'question',result);qid=q.id
    while True:
        with draft.WRITE_LOCK,Session.begin() as s:
            q=require(s,ChatQuestion,qid);meta=require(s,WorkbenchMeta,'business-question:'+qid)
            if q.status!='pending':return {'status':q.status,'question':q.prompt,'answer':meta.data.get('answer'),'approval':False}
            active=s.get(WorkbenchMeta,'active-chat:'+did)
            message=s.get(Message,mid)
            if not active or active.data.get('turn_id')!=turn or not message or message.status!='running':
                q.status='interrupted';draft.emit(s,did,'question',obj(q));return {'status':'interrupted','approval':False}
        time.sleep(.25)


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
        active=s.get(WorkbenchMeta,'active-chat:'+did)
        if not active or active.data.get('turn_id')!=q.turn_id:raise ValueError('此问题已过期，请继续对话')
        meta.data={**meta.data,'answer':payload};q.status='skipped' if skip else 'answered';q.answered_at=now()
        q.selected_label='；'.join(payload['labels']+[text.strip()]).strip('；')
        q.selected_option_id=json.dumps(selected)
        result={**obj(q),'business':True,'multiple':meta.data['multiple']};draft.emit(s,did,'question',result)
        return result
