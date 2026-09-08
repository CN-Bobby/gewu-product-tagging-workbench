"""A long-lived ACP process and persisted native session per dataset."""
import threading
from sqlalchemy import select
from .db import *
from .harness import ACP,DEFAULT_MODEL,instruction_hash
from . import draft,batch,tagging_rules

clients={}
busy={}
lock=threading.Lock()

def start(did,text,model=DEFAULT_MODEL,proposal_id=None,vision_model=None):
    if not text.strip() or len(text)>20000:raise ValueError('请输入1–20000字')
    if text.strip()=='确认执行':
        if not proposal_id:raise ValueError('请先选中需要确认的提案')
        jobs=draft.confirm(did,proposal_id,text_model=model,vision_model=vision_model)
        for j in jobs:batch.launch(j['id'])
        with draft.WRITE_LOCK,Session.begin() as s:
            for role,content in [('user',text),('assistant','已确认当前绑定提案，工作表和任务已更新。')]:
                m=Message(dataset_id=did,role=role,text=content);s.add(m);s.flush();draft.emit(s,did,'message',obj(m))
        return {'confirmed':True}
    with lock:
        if did in busy:raise ValueError('当前回复尚未完成，可先停止回复；批处理不影响继续对话')
        busy[did]=True
    try:
        with draft.WRITE_LOCK,Session.begin() as s:
            require(s,Dataset,did)
            m=Message(dataset_id=did,role='user',text=text);s.add(m);s.flush();draft.emit(s,did,'message',obj(m))
            m=Message(dataset_id=did,role='assistant',text='',status='running');s.add(m);s.flush();ident=m.id;draft.emit(s,did,'message',obj(m))
        threading.Thread(target=run,args=(did,ident,text,model),daemon=True).start()
        return {'message_id':ident}
    except Exception:
        with lock:busy.pop(did,None)
        raise

def run(did,ident,text,model):
    def event(e):
        with draft.WRITE_LOCK,Session.begin() as s:
            m=require(s,Message,ident)
            if e['type']=='text':m.text+=e['text'];draft.emit(s,did,'chat_chunk',{'id':ident,'text':e['text']})
            else:m.events=(m.events+[e])[-50:];draft.emit(s,did,'message',obj(m))
    try:
        client=clients.get(did)
        if client and getattr(client,'instruction_hash',None)!=instruction_hash():
            client.close();clients.pop(did);client=None
        if client and (client.closed or client.proc.poll() is not None):clients.pop(did);client=None
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
            m=require(s,Message,ident);m.status='completed';draft.emit(s,did,'message',obj(m))
    except Exception as e:
        with draft.WRITE_LOCK,Session.begin() as s:
            m=require(s,Message,ident);m.status='failed';m.text+='\n回复失败：'+str(e)[:600];draft.emit(s,did,'message',obj(m))
    finally:
        with lock:busy.pop(did,None)

def stop(did):
    if did in clients:clients[did].cancel()
    return {'stopping':did in busy}

def close():
    for c in list(clients.values()):c.cancel();c.close()
    clients.clear()
