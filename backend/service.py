import re
from collections import Counter
from sqlalchemy import select, func
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from .db import *

class Definition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    name: str = Field(min_length=1, max_length=80)
    type: Literal['FACT','INTERPRETATION']
    definition: str = Field(min_length=5, max_length=3000)
    options: list[str] = Field(default_factory=list, max_length=100)
    source: Literal['text','image','both','manual'] = 'both'

class Value(BaseModel):
    model_config=ConfigDict(extra='forbid')
    key: str
    raw_value: str | None
    normalized_value: str | None
    confidence: float | None = Field(ge=0, le=1)
    source: Literal['text','image','both','unknown']
    evidence: str = Field(min_length=1, max_length=4000)
    asset_ids: list[str] = Field(default_factory=list)

def list_datasets():
    with Session() as s:
        return [{**obj(d), 'count':s.scalar(select(func.count()).select_from(Product).where(Product.dataset_id==d.id)), 'image_count':s.scalar(select(func.count()).select_from(Asset).join(Product).where(Product.dataset_id==d.id, Asset.path.is_not(None)))} for d in s.scalars(select(Dataset).order_by(Dataset.created_at.desc()))]

def products(dataset_id, offset=0, limit=50):
    with Session() as s:
        require(s,Dataset,dataset_id)
        ps=s.scalars(select(Product).where(Product.dataset_id==dataset_id).order_by(Product.row).offset(offset).limit(min(limit,100))).all()
        return [{**obj(p),'images':[obj(a) for a in s.scalars(select(Asset).where(Asset.product_id==p.id))]} for p in ps]

def product_detail(dataset_id, product_id):
    with Session() as s:
        p=require(s,Product,product_id)
        if p.dataset_id!=dataset_id: raise ValueError('商品不属于当前数据集')
        return {**obj(p), 'images':[obj(a) for a in s.scalars(select(Asset).where(Asset.product_id==p.id))]}

def state(dataset_id):
    with Session() as s:
        d=require(s,Dataset,dataset_id)
        schemas=[obj(x) for x in s.scalars(select(Schema).where(Schema.dataset_id==dataset_id).order_by(Schema.version.desc()))]
        runs=[]
        for r in s.scalars(select(Run).where(Run.dataset_id==dataset_id).order_by(Run.created_at.desc())):
            tags=s.scalars(select(Tag).where(Tag.run_id==r.id)).all()
            runs.append({**obj(r),'done':len(tags),'reviewed':sum(x.review=='accepted' for x in tags),'total':len(r.product_ids)})
        return {'dataset':obj(d),'schemas':schemas,'runs':runs,
            'messages':[obj(x) for x in s.scalars(select(Message).where(Message.dataset_id==dataset_id).order_by(Message.created_at))],
            'transforms':[obj(x) for x in s.scalars(select(Transform).join(Run).where(Run.dataset_id==dataset_id).order_by(Transform.created_at))],
            'audit':[obj(x) for x in s.scalars(select(Audit).where(Audit.dataset_id==dataset_id).order_by(Audit.created_at.desc()).limit(80))]}

def propose(dataset_id, goal, definitions):
    if not goal or len(goal)>5000: raise ValueError('请提供有效分析目标')
    defs=[Definition.model_validate(d).model_dump() for d in definitions]
    if not 1<=len(defs)<=30: raise ValueError('标签维度应为 1–30 个')
    if len({d['key'] for d in defs})!=len(defs): raise ValueError('标签 key 不可重复')
    with Session.begin() as s:
        ds=require(s,Dataset,dataset_id)
        version=(s.scalar(select(func.max(Schema.version)).where(Schema.dataset_id==dataset_id)) or 0)+1
        schema=Schema(dataset_id=dataset_id,version=version,goal=goal,definitions=defs)
        s.add(schema);s.flush()
        ds.stage='TAG_PROPOSAL'
        audit(s,dataset_id,'schema_proposed',{'schema_id':schema.id,'version':version})
        return obj(schema)

def approve_schema(schema_id):
    with Session.begin() as s:
        schema=require(s,Schema,schema_id)
        if schema.status!='draft': raise ValueError('该版本已经确认，修改请创建新版本')
        schema.status='locked';schema.approved_at=now()
        require(s,Dataset,schema.dataset_id).stage='SCHEMA_LOCKED'
        audit(s,schema.dataset_id,'schema_approved',{'schema_id':schema_id})
        return obj(schema)

def create_run(dataset_id,schema_id,kind,model):
    if kind not in ('sample','full'): raise ValueError('无效任务类型')
    with Session.begin() as s:
        ds=require(s,Dataset,dataset_id);sc=require(s,Schema,schema_id)
        if sc.dataset_id!=dataset_id or sc.status!='locked': raise ValueError('请先确认当前数据集的标签版本')
        running=s.scalar(select(Run).where(Run.dataset_id==dataset_id,Run.status.in_(['running','queued'])))
        if running: raise ValueError('已有执行中的任务，请等待或暂停')
        parent=None
        if kind=='full':
            parent=s.scalar(select(Run).where(Run.schema_id==schema_id,Run.kind=='sample',Run.status=='completed',Run.approved_at.is_not(None),Run.model==model).order_by(Run.created_at.desc()))
            if not parent: raise ValueError('先完成并确认同一标签版本、同一模型的样本审核')
        ps=s.scalars(select(Product).where(Product.dataset_id==dataset_id).order_by(Product.row)).all()
        # Evenly spread across input order; deterministic, includes both ends.
        if kind=='sample' and len(ps)>20: ps=[ps[round(i*(len(ps)-1)/19)] for i in range(20)]
        run=Run(dataset_id=dataset_id,schema_id=schema_id,kind=kind,model=model,product_ids=[p.id for p in ps],parent_id=parent.id if parent else None)
        s.add(run);s.flush()
        if parent:
            for tag in s.scalars(select(Tag).where(Tag.run_id==parent.id)):
                s.add(Tag(run_id=run.id,product_id=tag.product_id,values=tag.values,corrected=tag.corrected,review=tag.review,review_note=tag.review_note))
        ds.stage='SAMPLE_RUN' if kind=='sample' else 'FULL_EXECUTION'
        audit(s,dataset_id,'run_created',{'run_id':run.id,'kind':kind,'count':len(ps),'model':model})
        return obj(run)

def record_tags(run_id, product_id, values):
    vals=[Value.model_validate(x).model_dump() for x in values]
    with Session.begin() as s:
        run=require(s,Run,run_id)
        if run.status not in ('running','queued'): raise ValueError('任务未运行，不接受迟到的结果')
        if product_id not in run.product_ids: raise ValueError('商品不属于任务')
        defs=require(s,Schema,run.schema_id).definitions
        if len(vals)!=len(defs) or {v['key'] for v in vals}!={d['key'] for d in defs}: raise ValueError('结果必须完整覆盖所有标签，未知项使用 null')
        allowed={a.id for a in s.scalars(select(Asset).where(Asset.product_id==product_id))}
        observations={o.asset_id for o in s.scalars(select(Observation).where(Observation.asset_id.in_(allowed),Observation.model==run.model))}
        for v in vals:
            definition=next(d for d in defs if d['key']==v['key'])
            if not set(v['asset_ids'])<=allowed: raise ValueError('图片证据不属于当前商品')
            if v['source'] in ('image','both') and not v['asset_ids']: raise ValueError('图片判断必须引用图片 ID')
            if v['source'] in ('image','both') and not set(v['asset_ids'])<=observations: raise ValueError('图片尚未成功通过当前模型分析，不能保存为视觉证据')
            if v['source']=='unknown' and (v['normalized_value'] is not None or v['raw_value'] is not None): raise ValueError('无证据项必须留空')
            if definition['source']=='image' and v['source']=='text' and v['normalized_value'] is not None: raise ValueError('图片维度不可用文本替代')
            if definition['options'] and v['normalized_value'] is not None and v['normalized_value'] not in definition['options']: raise ValueError('标准值不在已确认的选项中')
        existing=s.scalar(select(Tag).where(Tag.run_id==run_id,Tag.product_id==product_id))
        if existing: return {'saved':False,'reason':'已存在成功结果，保留原结果'}
        s.add(Tag(run_id=run_id,product_id=product_id,values=vals))
        audit(s,run.dataset_id,'tags_recorded',{'run_id':run_id,'product_id':product_id})
        return {'saved':True}

def run_results(run_id):
    with Session() as s:
        require(s,Run,run_id)
        return [{**obj(t),'product':obj(require(s,Product,t.product_id))} for t in s.scalars(select(Tag).where(Tag.run_id==run_id).order_by(Tag.created_at))]

def review_tag(tag_id, values=None, note=''):
    with Session.begin() as s:
        t=require(s,Tag,tag_id);run=require(s,Run,t.run_id)
        if run.approved_at: raise ValueError('已确认的样本审核不可覆盖，请创建新的样本任务')
        if values is not None:
            defs=require(s,Schema,run.schema_id).definitions
            if set(values)!={d['key'] for d in defs}: raise ValueError('校准值必须包含全部标签')
            for d in defs:
                value=values[d['key']]
                if value is not None and not isinstance(value,str): raise ValueError('校准值必须为文本或 null')
                if value is not None and d['options'] and value not in d['options']: raise ValueError('校准值不在标签选项中；修改规则请创建新版本')
            t.corrected=values
        t.review='accepted';t.review_note=note
        audit(s,run.dataset_id,'sample_reviewed',{'tag_id':tag_id,'corrections':values,'note':note})
        return obj(t)

def approve_sample(run_id):
    with Session.begin() as s:
        r=require(s,Run,run_id)
        if r.kind!='sample' or r.status!='completed': raise ValueError('样本任务尚未全部成功')
        tags=s.scalars(select(Tag).where(Tag.run_id==r.id)).all()
        if len(tags)!=len(r.product_ids) or any(t.review!='accepted' for t in tags): raise ValueError('请逐条审核所有样本后再确认')
        r.approved_at=now();require(s,Dataset,r.dataset_id).stage='CALIBRATED'
        audit(s,r.dataset_id,'sample_approved',{'run_id':r.id})
        return obj(r)

def effective(s, run_id, tag):
    result={v['key']:(tag.corrected[v['key']] if tag.corrected is not None else v['normalized_value']) for v in tag.values}
    for t in s.scalars(select(Transform).where(Transform.run_id==run_id,Transform.status=='approved').order_by(Transform.created_at)):
        result[t.output_key]=t.mapping.get(result.get(t.source_key))
    return result

def distribution(run_id):
    with Session() as s:
        require(s,Run,run_id)
        counts={}
        for t in s.scalars(select(Tag).where(Tag.run_id==run_id)):
            for k,v in effective(s,run_id,t).items(): counts.setdefault(k,Counter())[v if v is not None else '（未知）']+=1
        return {k:dict(v) for k,v in counts.items()}

def propose_transform(run_id, source_key, output_key, definition, mapping, tag_type='INTERPRETATION'):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',output_key): raise ValueError('输出 key 应为小写英文与下划线')
    if tag_type not in ('FACT','INTERPRETATION') or len(definition)<5: raise ValueError('请定义转换类型与业务边界')
    if not mapping or not all(isinstance(k,str) and isinstance(v,str) and v for k,v in mapping.items()): raise ValueError('映射应为非空的 文本:文本 对象')
    with Session.begin() as s:
        r=require(s,Run,run_id)
        if r.status!='completed': raise ValueError('请等待该任务全部完成后再做标签转换')
        defs=require(s,Schema,r.schema_id).definitions
        keys={d['key'] for d in defs}|{t.output_key for t in s.scalars(select(Transform).where(Transform.run_id==run_id,Transform.status=='approved'))}
        if source_key not in keys or output_key in keys: raise ValueError('来源不存在，或输出维度已存在；请使用新名称保留旧维度')
        known={effective(s,run_id,t).get(source_key) for t in s.scalars(select(Tag).where(Tag.run_id==run_id))}-{None}
        if not known<=mapping.keys(): raise ValueError('映射未覆盖全部已知值：'+str(known-mapping.keys()))
        t=Transform(run_id=run_id,source_key=source_key,output_key=output_key,definition=definition,mapping=mapping,type=tag_type)
        s.add(t);s.flush();audit(s,r.dataset_id,'transformation_proposed',{'transform_id':t.id})
        return obj(t)

def approve_transform(ident):
    with Session.begin() as s:
        t=require(s,Transform,ident);r=require(s,Run,t.run_id)
        if t.status!='draft': raise ValueError('该转换已确认')
        if s.scalar(select(Transform).where(Transform.run_id==r.id,Transform.output_key==t.output_key,Transform.status=='approved')): raise ValueError('输出维度已存在')
        t.status='approved';t.approved_at=now()
        s.add(Experience(workspace_id=require(s,Dataset,r.dataset_id).workspace_id,transform_id=t.id,problem=t.definition,solution={'source':t.source_key,'output':t.output_key,'mapping':t.mapping,'reference_only':True}))
        audit(s,r.dataset_id,'transformation_approved',{'transform_id':t.id})
        return obj(t)
