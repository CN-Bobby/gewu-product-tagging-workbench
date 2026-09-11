"""One persistent Reasonix session per dataset, including resumable web questions."""
import copy
import threading
from sqlalchemy import select
from .db import *
from .harness import ACP,DEFAULT_MODEL,HarnessError,ERROR_MESSAGES,instruction_hash
from . import draft,batch
from .conversation_presentation import build as build_presentation

clients={}
busy={}
lock=threading.Lock()

def _active_meta(s,did,turn_id,message_id):
    key='active-chat:'+did
    item=s.get(WorkbenchMeta,key)
    data={'turn_id':turn_id,'message_id':message_id}
    if item:item.kind='active_chat_turn';item.dataset_id=did;item.data=data;item.updated_at=now()
    else:s.add(WorkbenchMeta(id=key,dataset_id=did,kind='active_chat_turn',data=data))

def _clear_active(s,did,turn_id):
    item=s.get(WorkbenchMeta,'active-chat:'+did)
    if item and item.kind=='active_chat_turn' and (item.data or {}).get('turn_id')==turn_id:s.delete(item)

def _question_record(s,did,turn_id,message_id,event):
    question=s.get(ChatQuestion,event['id'])
    if question:return question
    question=ChatQuestion(
        id=event['id'],dataset_id=did,turn_id=turn_id,message_id=message_id,
        tool_call_id=event.get('tool_call_id'),question_key=event.get('question_key'),
        header=event.get('header'),prompt=event['prompt'],options=copy.deepcopy(event['options']),status='pending')
    s.add(question);s.flush();return question

def start(did,text,model=DEFAULT_MODEL,proposal_id=None,vision_model=None):
    if not text.strip() or len(text)>20000:raise ValueError('请输入1–20000字')
    turn_id=uid()
    if text.strip()=='确认执行':
        if not proposal_id:raise ValueError('请先选中需要确认的提案')
        confirmation_key='chat-confirm:'+proposal_id
        jobs=draft.confirm(did,proposal_id,text_model=model,vision_model=vision_model,confirmation_key=confirmation_key)
        for job in jobs:batch.launch(job['id'])
        with draft.WRITE_LOCK,Session.begin() as s:
            for role,content in [('user',text),('assistant','已确认当前绑定提案，工作表和任务已更新。')]:
                message=Message(dataset_id=did,turn_id=turn_id,role=role,text=content)
                if role=='assistant':message.presentation=build_presentation(content)
                s.add(message);s.flush();draft.emit(s,did,'message',obj(message))
        return {'confirmed':True,'jobs':jobs}
    with lock:
        if did in busy:raise ValueError('当前回复尚未完成，可先回答问题卡或停止回复；批处理不影响继续对话')
        busy[did]=turn_id
    try:
        with draft.WRITE_LOCK,Session.begin() as s:
            require(s,Dataset,did)
            user=Message(dataset_id=did,turn_id=turn_id,role='user',text=text);s.add(user);s.flush();draft.emit(s,did,'message',obj(user))
            response=Message(dataset_id=did,turn_id=turn_id,role='assistant',text='',status='running',presentation={});s.add(response);s.flush()
            ident=response.id;_active_meta(s,did,turn_id,ident);draft.emit(s,did,'message',obj(response))
        threading.Thread(target=run,args=(did,ident,turn_id,text,model),daemon=True).start()
        return {'message_id':ident,'turn_id':turn_id}
    except Exception:
        with lock:busy.pop(did,None)
        raise

def run(did,ident,turn_id,text,model):
    def event(item):
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident)
            if item['type']=='text':
                message.text=(message.text or '')+item['text']
                message.presentation=build_presentation(message.text,'running',events=message.events)
                draft.emit(s,did,'chat_chunk',{'id':ident,'text':item['text']})
                draft.emit(s,did,'message_presentation',{'id':ident,'presentation':message.presentation})
            elif item['type']=='question':
                question=_question_record(s,did,turn_id,ident,item)
                message.events=(message.events+[{'type':'question','id':question.id,'title':'等待你的选择'}])[-100:]
                draft.emit(s,did,'question',obj(question));draft.emit(s,did,'message',obj(message))
            elif item['type']=='question_interrupted':
                question=s.get(ChatQuestion,item['id'])
                if question and question.status=='pending':
                    question.status='interrupted';question.error_code=item.get('code') or 'reasonix_process_error'
                    draft.emit(s,did,'question',obj(question))
            else:
                safe={k:v for k,v in item.items() if k not in ('rawInput','content')}
                message.events=(message.events+[safe])[-100:];draft.emit(s,did,'message',obj(message))
    try:
        client=clients.get(did)
        if client and getattr(client,'instruction_hash',None)!=instruction_hash():
            client.close();clients.pop(did,None);client=None
        if client and (client.closed or client.proc.poll() is not None):clients.pop(did,None);client=None
        if not client:
            with Session() as s:
                saved=s.scalar(select(ChatSession).where(ChatSession.dataset_id==did));session_id=saved.session_id if saved else None
                if saved and saved.model!=model:model=saved.model
            client=ACP(did,model,on_event=event,session_id=session_id);clients[did]=client
            client.instruction_hash=instruction_hash()
            with draft.WRITE_LOCK,Session.begin() as s:
                saved=s.scalar(select(ChatSession).where(ChatSession.dataset_id==did))
                if not saved:s.add(ChatSession(dataset_id=did,session_id=client.session_id,model=model))
                audit(s,did,'reasonix_session_resumed' if session_id else 'reasonix_session_created',{'session_id':client.session_id,'model':model})
        client.on_event=event
        client.prompt(text)
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident);message.status='completed'
            message.presentation=build_presentation(message.text,message.status,events=message.events)
            draft.emit(s,did,'message_presentation',{'id':ident,'presentation':message.presentation})
            draft.emit(s,did,'message',obj(message))
    except HarnessError as exc:
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident)
            message.status='interrupted' if exc.code in ('interaction_cancelled','unsupported_question') else 'failed'
            message.error_code=exc.code;message.technical=exc.technical
            message.presentation=build_presentation(message.text,message.status,exc.code,exc.message,exc.technical,message.events)
            for question in s.scalars(select(ChatQuestion).where(ChatQuestion.message_id==ident,ChatQuestion.status=='pending')):
                question.status='cancelled' if exc.code=='interaction_cancelled' else 'interrupted';question.error_code=exc.code
                draft.emit(s,did,'question',obj(question))
            draft.emit(s,did,'message_presentation',{'id':ident,'presentation':message.presentation,'error_code':exc.code})
            draft.emit(s,did,'message',obj(message))
    except Exception as exc:
        technical={'exception_type':type(exc).__name__,'detail':str(exc)[:800]}
        code='reasonix_process_error' if '进程' in str(exc) or '连接已断开' in str(exc) else 'unknown_acp_error'
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident);message.status='failed';message.error_code=code;message.technical=technical
            message.presentation=build_presentation(message.text,message.status,code,ERROR_MESSAGES[code],technical,message.events)
            draft.emit(s,did,'message_presentation',{'id':ident,'presentation':message.presentation,'error_code':code})
            draft.emit(s,did,'message',obj(message))
    finally:
        with draft.WRITE_LOCK,Session.begin() as s:_clear_active(s,did,turn_id)
        with lock:
            if busy.get(did)==turn_id:busy.pop(did,None)

def answer(did,qid,option_id):
    with draft.WRITE_LOCK,Session.begin() as s:
        question=require(s,ChatQuestion,qid)
        if question.dataset_id!=did:raise ValueError('问题不属于当前数据集')
        if question.status=='answered':
            if question.selected_option_id!=option_id:raise ValueError('这个问题已经选择过其他答案')
            return obj(question)
        if question.status!='pending':raise ValueError('这个问题已经结束，请重新发送最后一条消息')
        offered={x.get('option_id'):x for x in question.options or []}
        if option_id not in offered:raise ValueError('所选答案不属于当前问题')
        client=clients.get(did)
        if not client or client.closed or client.proc.poll() is not None:
            question.status='interrupted';question.error_code='interaction_bridge_error';draft.emit(s,did,'question',obj(question))
            raise ValueError(ERROR_MESSAGES['interaction_bridge_error'])
        client.answer_question(qid,option_id)
        question.status='answered';question.selected_option_id=option_id;question.selected_label=offered[option_id].get('label');question.answered_at=now()
        audit(s,did,'chat_question_answered',{'question_id':qid,'turn_id':question.turn_id,'option_id':option_id})
        draft.emit(s,did,'question',obj(question));return obj(question)

def stop(did):
    client=clients.get(did)
    if client:client.cancel()
    with draft.WRITE_LOCK,Session.begin() as s:
        for question in s.scalars(select(ChatQuestion).where(ChatQuestion.dataset_id==did,ChatQuestion.status=='pending')):
            question.status='cancelled';question.error_code='interaction_cancelled';draft.emit(s,did,'question',obj(question))
    return {'stopping':did in busy}

def close():
    for client in list(clients.values()):client.cancel();client.close()
    clients.clear()
