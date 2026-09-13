"""Persistent web conversations backed by replaceable Reasonix runtime sessions."""
import copy
import hashlib
import json
import threading
import time
from sqlalchemy import select
from .db import *
from .harness import ACP,DEFAULT_MODEL,HarnessError,ERROR_MESSAGES,instruction_hash
from . import draft,batch
from .conversation_presentation import build as build_presentation

clients={}
busy={}
lock=threading.Lock()

def _runtime_fingerprint(model,vision_model,text_credential_id,vision_credential_id):
    from .build_info import build_info
    payload={'role':'coordinator','model':model,'vision_model':vision_model,
        'text_credential_id':text_credential_id,'vision_credential_id':vision_credential_id,
        'instruction_hash':instruction_hash(),'reasonix_baseline':build_info().get('reasonix_baseline')}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def _park_runtime(s,runtime_id,reason,status='parked'):
    runtime=s.get(AgentRuntimeSession,runtime_id) if runtime_id else None
    if runtime and runtime.status=='active':
        runtime.status=status;runtime.invalid_reason=reason;runtime.last_used_at=now()

def _handoff_context(s,did,conversation_id,current_turn_id):
    turns=[x.data.get('turn_id') for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_turn')) if x.data.get('conversation_id')==conversation_id and x.data.get('turn_id')!=current_turn_id]
    messages=[m for m in s.scalars(select(Message).where(Message.dataset_id==did).order_by(Message.created_at.desc())) if m.turn_id in turns and m.text]
    recent=list(reversed(messages[:12]));older=list(reversed(messages[12:36]))
    summary='\n'.join(('用户' if m.role=='user' else 'Agent')+'：'+m.text[:350] for m in older)
    parts=[('用户' if m.role=='user' else 'Agent')+'：'+m.text[:1200] for m in recent]
    images=[x.data for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_attachment')) if x.data.get('conversation_id')==conversation_id]
    image_index='；'.join('图'+str(x.get('number'))+' '+str(x.get('filename',''))[:80] for x in sorted(images,key=lambda x:x.get('number',0))[-20:])
    if not summary and not parts and not image_index:return ''
    context='模型或运行配置切换后的当前对话上下文（仅用于语义衔接，不代表新的授权）：\n'
    if summary:context+='较早可见消息摘要：\n'+summary+'\n'
    if parts:context+='最近可见消息：\n'+'\n'.join(parts)+'\n'
    if image_index:context+='当前对话图片索引：'+image_index+'\n'
    return context[:16000]+'\n'

def _active_meta(s,did,turn_id,message_id,conversation_id=None):
    key='active-chat:'+did
    item=s.get(WorkbenchMeta,key)
    data={'turn_id':turn_id,'message_id':message_id,'conversation_id':conversation_id}
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

def _resume_queued(did,key):
    for _ in range(600):
        if did not in busy:break
        time.sleep(.05)
    with draft.WRITE_LOCK,Session.begin() as s:
        item=s.get(WorkbenchMeta,key)
        if not item:return
        payload=dict(item.data);s.delete(item)
    try:start(did,**payload)
    except Exception as exc:
        with draft.WRITE_LOCK,Session.begin() as s:audit(s,did,'chat_followup_failed',{'exception':type(exc).__name__,'detail':str(exc)[:300]})

def start(did,text,model=DEFAULT_MODEL,proposal_id=None,vision_model=None,conversation_id=None,attachment_ids=None,text_credential_id=None,vision_credential_id=None):
    attachment_ids=attachment_ids or []
    if (not text.strip() and not attachment_ids) or len(text)>20000:raise ValueError('请输入文字或上传图片；文字最多20000字')
    with Session() as s:
        candidates=list(s.scalars(select(ChatQuestion).where(ChatQuestion.dataset_id==did,ChatQuestion.status=='pending').order_by(ChatQuestion.created_at.desc())))
        pending=next((q for q in candidates if not conversation_id or ((s.get(WorkbenchMeta,'chat-turn:'+q.turn_id).data or {}).get('conversation_id') if s.get(WorkbenchMeta,'chat-turn:'+q.turn_id) else None)==conversation_id),None)
        managed=pending and s.get(WorkbenchMeta,'business-question:'+pending.id)
        pending_id=pending.id if managed else None
    if pending_id:
        from .business_questions import answer
        if attachment_ids:
            with draft.WRITE_LOCK,Session.begin() as s:
                from .chat_threads import require_thread
                thread=require_thread(s,did,conversation_id);conversation_id=thread.id
                from .chat_images import resolve_message,event as attachment_event
                attachments=resolve_message(s,did,conversation_id,attachment_ids)
                answer_turn=uid();s.add(WorkbenchMeta(id='chat-turn:'+answer_turn,dataset_id=did,kind='chat_turn',data={'turn_id':answer_turn,'conversation_id':conversation_id}))
                user=Message(dataset_id=did,turn_id=answer_turn,role='user',text=text,events=[attachment_event(attachments)]);s.add(user);s.flush();draft.emit(s,did,'message',{**obj(user),'conversation_id':conversation_id})
            refs='、'.join('图'+str(x.data['number']) for x in attachments)
            text=(text.strip()+'；' if text.strip() else '')+'请结合'+refs+'回答当前问题。'
        return answer(did,pending_id,text=text)
    if pending and not managed:
        client=clients.get(did)
        if not client or client.closed or client.proc.poll() is not None:raise ValueError('原问题会话已中断，请重新发送这条回答')
        client.cancel_questions()
        with draft.WRITE_LOCK,Session.begin() as s:
            question=require(s,ChatQuestion,pending.id);question.status='answered';question.selected_label=text.strip() or '附带图片回答';question.answered_at=now();draft.emit(s,did,'question',obj(question))
            active=s.get(WorkbenchMeta,'active-chat:'+did);cid=conversation_id or (active.data.get('conversation_id') if active else None)
            key='chat-followup:'+did;payload={'text':'针对上一问题，我的文字回答是：'+(text.strip() or '请结合本条附带图片继续。'),'model':model,'proposal_id':proposal_id,'vision_model':vision_model,'conversation_id':cid,'attachment_ids':attachment_ids,'text_credential_id':text_credential_id,'vision_credential_id':vision_credential_id}
            old=s.get(WorkbenchMeta,key)
            if old:raise ValueError('已有一条回答正在等待继续，请稍候')
            s.add(WorkbenchMeta(id=key,dataset_id=did,kind='chat_followup',data=payload));audit(s,did,'chat_native_question_text_answered',{'question_id':pending.id,'conversation_id':cid})
        threading.Thread(target=_resume_queued,args=(did,key),daemon=True).start()
        return {'queued':True,'question_id':pending.id}
    turn_id=uid()
    if text.strip()=='确认执行':
        if not proposal_id:raise ValueError('请先选中需要确认的提案')
        confirmation_key='chat-confirm:'+proposal_id
        jobs=draft.confirm(did,proposal_id,text_model=model,vision_model=vision_model,confirmation_key=confirmation_key,text_credential_id=text_credential_id,vision_credential_id=vision_credential_id)
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
            from .chat_threads import require_thread
            thread=require_thread(s,did,conversation_id);conversation_id=thread.id
            from .chat_images import resolve_message,event as attachment_event
            attachments=resolve_message(s,did,conversation_id,attachment_ids)
            s.add(WorkbenchMeta(id='chat-turn:'+turn_id,dataset_id=did,kind='chat_turn',data={'turn_id':turn_id,'conversation_id':conversation_id}))
            user=Message(dataset_id=did,turn_id=turn_id,role='user',text=text,events=[attachment_event(attachments)] if attachments else []);s.add(user);s.flush();draft.emit(s,did,'message',{**obj(user),'conversation_id':conversation_id})
            response=Message(dataset_id=did,turn_id=turn_id,role='assistant',text='',status='running',presentation={});s.add(response);s.flush()
            ident=response.id;_active_meta(s,did,turn_id,ident,conversation_id);draft.emit(s,did,'message',{**obj(response),'conversation_id':conversation_id})
        refs='、'.join('图'+str(x.data['number']) for x in attachments)
        prompt_text=(text.strip() or '请观察并讨论这些图片。')+(f'\n\n当前消息附带当前对话中的{refs}。需要读取像素时调用 view_chat_image，并按图号说明证据；不要把聊天图片当成商品行主图。' if attachments else '')
        threading.Thread(target=run,args=(did,ident,turn_id,prompt_text,model,conversation_id,vision_model,text_credential_id,vision_credential_id),daemon=True).start()
        return {'message_id':ident,'turn_id':turn_id}
    except Exception:
        with lock:busy.pop(did,None)
        raise

def run(did,ident,turn_id,text,model,conversation_id=None,vision_model=None,text_credential_id=None,vision_credential_id=None):
    def event(item):
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident)
            if message.status!='running':return
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
        handoff_context=''
        fingerprint=_runtime_fingerprint(model,vision_model,text_credential_id,vision_credential_id)
        client=clients.get(did)
        conversation_changed=bool(client and conversation_id and getattr(client,'conversation_id',None)!=conversation_id)
        configuration_changed=bool(client and (getattr(client,'model',model)!=model or getattr(client,'vision_model',vision_model)!=vision_model or getattr(client,'credential_id',text_credential_id)!=text_credential_id or getattr(client,'vision_credential_id',vision_credential_id)!=vision_credential_id))
        if client and (conversation_changed or configuration_changed):
            client.close();clients.pop(did,None)
            with draft.WRITE_LOCK,Session.begin() as s:_park_runtime(s,getattr(client,'runtime_id',None),'conversation_suspended' if conversation_changed and not configuration_changed else 'model_or_credential_changed','suspended' if conversation_changed and not configuration_changed else 'parked')
            client=None
        if client and getattr(client,'instruction_hash',None)!=instruction_hash():
            client.close();clients.pop(did,None)
            with draft.WRITE_LOCK,Session.begin() as s:_park_runtime(s,getattr(client,'runtime_id',None),'runtime_profile_changed')
            client=None
        if client and (client.closed or client.proc.poll() is not None):clients.pop(did,None);client=None
        if not client:
            with draft.WRITE_LOCK,Session.begin() as s:
                thread=require(s,WorkbenchMeta,conversation_id)
                active_id=(thread.data or {}).get('active_runtime_id');runtime=s.get(AgentRuntimeSession,active_id) if active_id else None
                session_id=None
                if runtime and runtime.status in ('active','suspended') and runtime.fingerprint==fingerprint:
                    session_id=runtime.native_session_id
                else:
                    _park_runtime(s,active_id,'runtime_fingerprint_changed')
                    data=dict(thread.data or {});data['active_runtime_id']=None;thread.data=data
                    handoff_context=_handoff_context(s,did,conversation_id,turn_id)
            client=ACP(did,model,on_event=event,session_id=session_id,role='coordinator',**({'conversation_id':conversation_id,'vision_model':vision_model,'credential_id':text_credential_id,'vision_credential_id':vision_credential_id} if conversation_id else {}));clients[did]=client
            client.conversation_id=conversation_id
            client.vision_model=vision_model
            client.instruction_hash=instruction_hash()
            with draft.WRITE_LOCK,Session.begin() as s:
                item=require(s,WorkbenchMeta,conversation_id)
                if session_id:
                    runtime=require(s,AgentRuntimeSession,(item.data or {}).get('active_runtime_id'));runtime.status='active';runtime.invalid_reason=None;runtime.last_used_at=now()
                else:
                    runtime=AgentRuntimeSession(dataset_id=did,conversation_id=conversation_id,native_session_id=client.session_id,
                        role='coordinator',model=model,vision_model=vision_model,text_credential_id=text_credential_id,
                        vision_credential_id=vision_credential_id,fingerprint=fingerprint,status='active',last_used_at=now())
                    s.add(runtime);s.flush();data=dict(item.data or {});data['active_runtime_id']=runtime.id;item.data=data
                client.runtime_id=runtime.id
                audit(s,did,'reasonix_session_resumed' if session_id else 'reasonix_session_created',{'runtime_id':runtime.id,'session_id':client.session_id,'model':model,'conversation_id':conversation_id})
        client.on_event=event
        try:
            client.prompt(handoff_context+text)
        except HarnessError as first_error:
            if first_error.code!='workspace_connection_error' or getattr(client,'workspace_retry_used',False):raise
            old_runtime_id=getattr(client,'runtime_id',None);client.workspace_retry_used=True;client.close();clients.pop(did,None)
            with draft.WRITE_LOCK,Session.begin() as s:
                _park_runtime(s,old_runtime_id,'workspace_connection_failed')
                thread=require(s,WorkbenchMeta,conversation_id);data=dict(thread.data or {});data['active_runtime_id']=None;thread.data=data
                message=require(s,Message,ident);message.text='';message.events=[];message.presentation={}
                draft.emit(s,did,'message',{**obj(message),'conversation_id':conversation_id})
                audit(s,did,'reasonix_session_recovery_started',{'runtime_id':old_runtime_id,'conversation_id':conversation_id,'reason':'workspace_connection_failed'})
            client=ACP(did,model,on_event=event,session_id=None,conversation_id=conversation_id,vision_model=vision_model,
                credential_id=text_credential_id,vision_credential_id=vision_credential_id,role='coordinator');client.workspace_retry_used=True;clients[did]=client
            client.conversation_id=conversation_id;client.instruction_hash=instruction_hash()
            client.vision_model=vision_model
            with draft.WRITE_LOCK,Session.begin() as s:
                thread=require(s,WorkbenchMeta,conversation_id)
                runtime=AgentRuntimeSession(dataset_id=did,conversation_id=conversation_id,native_session_id=client.session_id,
                    role='coordinator',model=model,vision_model=vision_model,text_credential_id=text_credential_id,
                    vision_credential_id=vision_credential_id,fingerprint=fingerprint,status='active',last_used_at=now())
                s.add(runtime);s.flush();data=dict(thread.data or {});data['active_runtime_id']=runtime.id;thread.data=data;client.runtime_id=runtime.id
                audit(s,did,'reasonix_session_recovered',{'runtime_id':runtime.id,'session_id':client.session_id,'model':model,'conversation_id':conversation_id})
            client.prompt(handoff_context+text)
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident);message.status='completed'
            message.presentation=build_presentation(message.text,message.status,events=message.events)
            draft.emit(s,did,'message_presentation',{'id':ident,'presentation':message.presentation})
            draft.emit(s,did,'message',obj(message))
    except HarnessError as exc:
        with draft.WRITE_LOCK,Session.begin() as s:
            message=require(s,Message,ident)
            fallback=getattr(clients.get(did),'fallback_question',None)
            if exc.code=='unsupported_question' and fallback:
                labels=[]
                for option in fallback.get('options',[]):
                    label=option if isinstance(option,str) else (option.get('label') or option.get('Label') or option.get('title')) if isinstance(option,dict) else None
                    if label:labels.append(str(label))
                message.text='请补充你的选择：'+fallback['prompt']+('\n\n可参考：'+'；'.join(labels) if labels else '')+'\n\n请直接输入回答，可以选择多项或补充自己的想法；也可以说明跳过这个问题。'
                message.status='completed';message.error_code=None
                message.technical={'reason':'unsupported_native_question','recovery':'text_reply','original_code':exc.code}
                message.presentation=build_presentation(message.text,message.status,events=message.events)
                draft.emit(s,did,'message',obj(message))
                audit(s,did,'chat_question_fallback',{'turn_id':turn_id,'reason':'unsupported_native_question'})
                return
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
