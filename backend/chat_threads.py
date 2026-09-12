"""Dataset-owned conversations stored using the existing versioned metadata table."""
import shutil
from pathlib import Path
from sqlalchemy import select
from .db import *
from . import draft


def ensure(s,did):
    require(s,Dataset,did)
    marker='chat-migrated:'+did
    if not s.get(WorkbenchMeta,marker):
        saved=s.scalar(select(ChatSession).where(ChatSession.dataset_id==did))
        ident=uid()
        s.add(WorkbenchMeta(id=ident,dataset_id=did,kind='chat_thread',data={'name':'历史对话','deleted':False,'legacy':True,'session_id':saved.session_id if saved else None,'model':saved.model if saved else None,'draft':''}))
        s.add(WorkbenchMeta(id=marker,dataset_id=did,kind='chat_migration',data={'default_id':ident}))
        s.flush()
    return s.get(WorkbenchMeta,marker).data['default_id']


def require_thread(s,did,cid=None):
    default=ensure(s,did)
    item=require(s,WorkbenchMeta,cid or default)
    if item.dataset_id!=did or item.kind!='chat_thread' or item.data.get('deleted'):raise ValueError('对话不存在或已移入回收站')
    return item


def listing(did):
    with draft.WRITE_LOCK,Session.begin() as s:
        ensure(s,did)
        return [{**obj(x),**x.data} for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_thread').order_by(WorkbenchMeta.updated_at,WorkbenchMeta.id))]


def create(did,name='新对话'):
    if not isinstance(name,str) or not 1<=len(name.strip())<=100:raise ValueError('对话名称需为1–100字')
    with draft.WRITE_LOCK,Session.begin() as s:
        ensure(s,did);item=WorkbenchMeta(id=uid(),dataset_id=did,kind='chat_thread',data={'name':name.strip(),'deleted':False,'legacy':False,'session_id':None,'model':None,'draft':''})
        s.add(item);s.flush();return {**obj(item),**item.data}


def update(did,cid,body):
    with draft.WRITE_LOCK,Session.begin() as s:
        item=require(s,WorkbenchMeta,cid)
        if item.dataset_id!=did or item.kind!='chat_thread':raise ValueError('对话不属于此表格')
        data=dict(item.data)
        if 'name' in body:
            name=body['name']
            if not isinstance(name,str) or not 1<=len(name.strip())<=100:raise ValueError('对话名称需为1–100字')
            data['name']=name.strip()
        if 'draft' in body:
            if not isinstance(body['draft'],str) or len(body['draft'])>20000:raise ValueError('草稿最多20000字')
            data['draft']=body['draft']
        for field in ('model','vision_model','text_credential_id','vision_credential_id'):
            if field not in body:continue
            value=body[field]
            if value is not None and (not isinstance(value,str) or len(value)>300):raise ValueError('对话模型配置无效')
            data[field]=value or None
        if 'deleted' in body:
            if type(body['deleted']) is not bool:raise ValueError('回收站状态无效')
            active=s.get(WorkbenchMeta,'active-chat:'+did)
            if active and active.data.get('conversation_id')==cid:raise ValueError('请先停止此对话的回复，再移入回收站')
            data['deleted']=body['deleted']
            if data['deleted']:
                turns=list(s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_turn')))
                ids=[t.data['turn_id'] for t in turns if t.data.get('conversation_id')==cid]
                for p in s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.origin_turn_id.in_(ids),Proposal.status=='pending')):p.status='superseded'
        item.data=data;item.updated_at=now();return {**obj(item),**data}


def destroy(did,cid):
    """Permanently delete one recycled conversation without touching shared results."""
    session_id=None;attachment_paths=[];preserved_attachments=0
    with draft.WRITE_LOCK,Session.begin() as s:
        item=require(s,WorkbenchMeta,cid)
        if item.dataset_id!=did or item.kind!='chat_thread':raise ValueError('对话不属于此表格')
        if not item.data.get('deleted'):raise ValueError('请先把对话移入回收站，再彻底删除')
        active=s.get(WorkbenchMeta,'active-chat:'+did)
        if active and active.data.get('conversation_id')==cid:raise ValueError('此对话仍在回复，不能删除')
        session_id=item.data.get('session_id')
        turns=[x for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_turn')) if x.data.get('conversation_id')==cid]
        turn_ids=[x.data.get('turn_id') for x in turns]
        messages=list(s.scalars(select(Message).where(Message.dataset_id==did,Message.turn_id.in_(turn_ids)))) if turn_ids else []
        message_ids=[x.id for x in messages]
        questions=list(s.scalars(select(ChatQuestion).where(ChatQuestion.message_id.in_(message_ids)))) if message_ids else []
        for question in questions:
            managed=s.get(WorkbenchMeta,'business-question:'+question.id)
            if managed:s.delete(managed)
            s.delete(question)
        for message in messages:s.delete(message)
        for turn in turns:s.delete(turn)
        attachments=[x for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_attachment')) if x.data.get('conversation_id')==cid]
        for attachment in attachments:
            # Once bound, the original file is evidence for a shared product asset and must survive chat deletion.
            if attachment.data.get('asset_id'):
                preserved_attachments+=1
                continue
            attachment_paths.extend([attachment.data.get('path'),attachment.data.get('thumb_path')])
            s.delete(attachment)
        s.delete(item)
        audit(s,did,'chat_conversation_destroyed',{'conversation_id':cid,'turn_count':len(turn_ids),'deleted_unbound_attachments':len(attachment_paths)//2,'preserved_bound_attachments':preserved_attachments})
    image_root=(STORAGE/'chat-images'/did/cid).resolve()
    for value in attachment_paths:
        if not value:continue
        path=Path(value).resolve()
        if path.parent==image_root and path.is_file():path.unlink()
    if image_root.is_dir() and not any(image_root.iterdir()):image_root.rmdir()
    runtime=(STORAGE/'harness'/did/('chat-'+cid)).resolve()
    harness_root=(STORAGE/'harness'/did).resolve()
    if runtime.parent==harness_root and runtime.is_dir():shutil.rmtree(runtime)
    if session_id and __import__('re').fullmatch(r'[a-zA-Z0-9_-]+',str(session_id)):
        session_root=(STORAGE/'datasets'/did/'reasonix'/'sessions').resolve()
        if session_root.is_dir():
            for path in session_root.glob(str(session_id)+'.*'):
                resolved=path.resolve()
                if resolved.parent==session_root:
                    if resolved.is_dir():shutil.rmtree(resolved)
                    elif resolved.is_file():resolved.unlink()
    return {'deleted':True,'conversation_id':cid}


def decorate(s,did,data):
    default=ensure(s,did)
    turns={x.data['turn_id']:x.data['conversation_id'] for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_turn'))}
    for m in data.get('messages',[]):m['conversation_id']=turns.get(m.get('turn_id'),default)
    for p in data.get('proposals',[]):p['conversation_id']=turns.get(p.get('origin_turn_id'),default)
