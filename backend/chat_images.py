"""Conversation-scoped image attachments with stable 图N numbering."""
import hashlib
import io
from pathlib import Path
from PIL import Image,UnidentifiedImageError
from sqlalchemy import select
from .db import *
from . import draft

MAX_COUNT=10
MAX_FILE=20*1024*1024
MAX_TOTAL=50*1024*1024
MAX_PIXELS=40_000_000
FORMATS={'PNG':('png','image/png'),'JPEG':('jpg','image/jpeg'),'WEBP':('webp','image/webp')}


def _items(s,did,cid):
    return [x for x in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='chat_attachment')) if x.data.get('conversation_id')==cid]


def upload(did,cid,files):
    if not 1<=len(files)<=MAX_COUNT:raise ValueError('每条消息可上传1–10张图片')
    total=sum(len(data) for _,data in files)
    if total>MAX_TOTAL:raise ValueError('本次图片合计不能超过50MB')
    prepared=[]
    for filename,data in files:
        if not data or len(data)>MAX_FILE:raise ValueError(f'{filename or "图片"}大小无效或超过20MB')
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt=im.format;width,height=im.size
                if fmt not in FORMATS:raise ValueError('仅支持 PNG、JPEG、WebP 图片')
                if width*height>MAX_PIXELS:raise ValueError('图片解码像素不能超过4000万')
                im.verify()
        except (UnidentifiedImageError,OSError) as exc:raise ValueError(f'{filename or "图片"}不是可读取的图片') from exc
        ext,mime=FORMATS[fmt];prepared.append((filename or 'image.'+ext,data,ext,mime,width,height,hashlib.sha256(data).hexdigest()))
    result=[]
    with draft.WRITE_LOCK,Session.begin() as s:
        from .chat_threads import require_thread
        require_thread(s,did,cid);existing=_items(s,did,cid);number=max([x.data.get('number',0) for x in existing] or [0])
        by_hash={x.data.get('sha256'):x for x in existing}
        root=STORAGE/'chat-images'/did/cid;root.mkdir(parents=True,exist_ok=True)
        for filename,data,ext,mime,width,height,digest in prepared:
            if digest in by_hash:
                item=by_hash[digest];result.append({**obj(item),**item.data});continue
            number+=1;ident=uid();path=root/(ident+'.'+ext);thumb=root/(ident+'.thumb.jpg')
            path.write_bytes(data)
            with Image.open(io.BytesIO(data)) as im:
                preview=im.convert('RGB');preview.thumbnail((480,480));preview.save(thumb,'JPEG',quality=82)
            payload={'conversation_id':cid,'number':number,'filename':filename[:255],'mime':mime,'path':str(path),'thumb_path':str(thumb),'sha256':digest,'size':len(data),'width':width,'height':height,'bound_product_id':None,'asset_id':None}
            item=WorkbenchMeta(id=ident,dataset_id=did,kind='chat_attachment',data=payload);s.add(item);s.flush();by_hash[digest]=item;result.append({**obj(item),**payload})
        audit(s,did,'chat_images_uploaded',{'conversation_id':cid,'attachment_ids':[x['id'] for x in result]})
    return result


def require_attachment(s,did,cid,ident):
    item=require(s,WorkbenchMeta,ident)
    if item.kind!='chat_attachment' or item.dataset_id!=did or item.data.get('conversation_id')!=cid:raise ValueError('图片不属于当前对话')
    return item


def resolve_message(s,did,cid,ids):
    if not isinstance(ids,list) or len(ids)>MAX_COUNT:raise ValueError('每条消息最多引用10张图片')
    seen=set();items=[]
    for ident in ids:
        if ident in seen:continue
        seen.add(ident);items.append(require_attachment(s,did,cid,ident))
    return items


def event(items):
    return {'type':'attachments','attachments':[{'id':x.id,'number':x.data['number'],'filename':x.data['filename'],'width':x.data['width'],'height':x.data['height']} for x in items]}


def bind(did,cid,ident,product_id):
    with draft.WRITE_LOCK,Session.begin() as s:
        item=require_attachment(s,did,cid,ident);product=require(s,Product,product_id)
        if product.dataset_id!=did:raise ValueError('商品不属于当前表格')
        data=dict(item.data)
        if data.get('asset_id'):
            asset=require(s,Asset,data['asset_id'])
            if asset.product_id!=product_id:raise ValueError('此图片已绑定其他商品，不能静默改绑')
            return {**obj(item),**data}
        asset=Asset(product_id=product_id,source=f'chat:{cid}:图{data["number"]}',path=data['path'],sha256=data['sha256'],image_type='supplemental')
        s.add(asset);s.flush();data.update(bound_product_id=product_id,asset_id=asset.id);item.data=data;item.updated_at=now()
        audit(s,did,'chat_image_bound',{'conversation_id':cid,'attachment_id':ident,'product_id':product_id,'asset_id':asset.id})
        return {**obj(item),**data}


def analyze_by_number(did,cid,number,question,model):
    with Session() as s:
        found=next((x for x in _items(s,did,cid) if x.data.get('number')==number),None)
        if not found:raise ValueError(f'当前对话不存在图{number}')
        data=found.data
    from .batch import observation
    import threading
    metrics={}
    asset={'id':'chat:'+found.id,'path':data['path'],'sha256':data['sha256']}
    result=observation(asset,model,(question or '围绕当前对话目标提取客观产品细节。')[:2000],False,metrics,threading.Lock())
    return {**result,'image_number':number,'attachment_id':found.id,'usage':metrics}
