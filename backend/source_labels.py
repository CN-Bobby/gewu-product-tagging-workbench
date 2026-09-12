"""Adopt an imported worksheet column as a protected, in-place label column."""
import hashlib
from sqlalchemy import select
from .db import *
from . import draft


def adopt(did,body):
    header=body.get('header');name=(body.get('name') or header or '').strip();definition=(body.get('definition') or '').strip()
    if not isinstance(header,str) or not header:raise ValueError('请选择已有标签列')
    if not name or len(definition)<5:raise ValueError('请填写标签名称和至少5个字的判断定义')
    options=body.get('options') or []
    if not isinstance(options,list) or any(not isinstance(x,str) or not x.strip() for x in options):raise ValueError('候选值格式无效')
    separator=body.get('separator')
    if separator is not None and (not isinstance(separator,str) or not separator or len(separator)>10):raise ValueError('多值分隔符需为1–10个字符')
    with draft.WRITE_LOCK,Session.begin() as s:
        dataset=require(s,Dataset,did)
        if header not in (dataset.headers or []):raise ValueError('来源列不存在')
        duplicate=next((c for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)) if c.definition.get('source_header')==header),None)
        if duplicate:raise ValueError('同一原列不能重复接入')
        key='imported_'+hashlib.sha256(header.encode()).hexdigest()[:12]
        d={'key':key,'name':name,'type':'FACT','source':'text','definition':definition,'options':list(dict.fromkeys(x.strip() for x in options)),'method':'model','cardinality':'multiple' if separator else 'single','result_shape':'multi_enum' if separator and options else 'scalar_enum' if options else 'scalar_open','source_header':header,'adopted_source':True,'adoption_active':True,'fill_empty_only':True,'separator':separator,'judgment':{'include':[definition],'exclude':[],'unknown':['来源值不符合定义或证据不足时不覆盖'],'fields':[header]}}
        col=DraftColumn(dataset_id=did,key=key,definition=d,origin={'source_header':header,'source':'imported_human_label','adopted_at':now()});s.add(col);s.flush()
        counts={'protected':0,'blank':0,'review':0}
        for product in s.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)):
            raw=(product.raw or {}).get(header);blank=raw is None or isinstance(raw,str) and not raw.strip()
            cell=Cell(product_id=product.id,column_id=col.id,revision=1,status='pending',value=None,raw_value=raw,manual=None,evidence={'source':'imported_human_label','source_header':header})
            if blank:counts['blank']+=1
            else:
                value=[x.strip() for x in raw.split(separator) if x.strip()] if separator and isinstance(raw,str) else raw
                valid=not options or (all(x in options for x in value) if isinstance(value,list) else value in options)
                cell.value=value;cell.manual={'value':value,'at':now(),'source':'imported_human_label'};cell.status='ready' if valid else 'review';cell.evidence={**cell.evidence,'protected':True,'definition_match':valid};counts['protected']+=1
                if not valid:counts['review']+=1
            s.add(cell)
        audit(s,did,'source_label_adopted',{'column_id':col.id,'source_header':header,**counts});draft.emit(s,did,'column',obj(col));draft.emit(s,did,'reload',{'reason':'已有标签列已接入'})
        return {**obj(col),'counts':counts}


def set_active(did,column_id,active):
    if type(active) is not bool:raise ValueError('接管状态无效')
    with draft.WRITE_LOCK,Session.begin() as s:
        col=require(s,DraftColumn,column_id)
        if col.dataset_id!=did or not col.definition.get('adopted_source'):raise ValueError('这不是接入的原始标签列')
        # Enabling/disabling controls presentation/export only; it must not stale preserved cell results.
        col.definition={**col.definition,'adoption_active':active};draft.emit(s,did,'column',obj(col));audit(s,did,'source_label_status',{'column_id':column_id,'active':active})
        return obj(col)
