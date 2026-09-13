"""Task lifecycle and writes are application-owned, independent of agent narration."""
from sqlalchemy import select
from .db import *

def settle_owned_token(s,did,token,reason='superseded'):
    """Close a token in its previous job after a newer job or a human takes ownership."""
    owner=s.get(WorkbenchMeta,'owner:'+token)
    if not owner:return None
    job=s.get(BatchJob,owner.data.get('job_id'))
    if not job or job.dataset_id!=did:return None
    completed=list(job.completed or [])
    if token not in completed:completed.append(token)
    errors=dict(job.errors or {});errors.pop(token,None)
    job.completed=completed;job.errors=errors
    metrics=dict(job.metrics or {});settled=dict(metrics.get('settled_tokens') or {})
    settled[token]=reason;metrics['settled_tokens']=settled;job.metrics=metrics
    expected=set((job.spec or {}).get('expected',{}))
    if expected and expected<=set(completed) and job.status in ('partial','paused','interrupted'):
        job.status='resolved'
    s.add(DraftEvent(dataset_id=did,kind='job',payload=brief(job)))
    return job

def claim(s,did,token,jid):
    key='owner:'+token;owner=s.get(WorkbenchMeta,key)
    if not owner:owner=WorkbenchMeta(id=key,dataset_id=did,kind='owner');s.add(owner)
    elif owner.data.get('job_id') and owner.data.get('job_id')!=jid:
        settle_owned_token(s,did,token,'由后续精确任务接管')
    owner.data={'job_id':jid};owner.updated_at=now()

def owns(s,job,token):
    owner=s.get(WorkbenchMeta,'owner:'+token)
    return not owner or owner.data.get('job_id')==job.id

def brief(job):
    j=obj(job) if not isinstance(job,dict) else job;spec=j['spec']
    counts=[]
    for col in spec.get('columns',[]):
        suffix=':'+col['id'];expected=[t for t in spec.get('expected',{}) if t.endswith(suffix)]
        counts.append({'column_id':col['id'],'name':col['definition']['name'],'expected':len(expected),'completed':sum(t in j['completed'] for t in expected),'failed':sum(t in j['errors'] for t in expected)})
    return {k:v for k,v in j.items() if k not in ('spec','errors','completed')}|{
        'spec':{k:spec.get(k) for k in ('kind','sample','text_model','vision_model')}|{'product_count':len(spec.get('product_ids',[])),'columns':[{'id':c['id'],'revision':c['revision'],'definition':{'name':c['definition']['name']}} for c in spec.get('columns',[])]},
        'completed_count':len(j['completed']),'expected_count':len(spec.get('expected',{})),'column_counts':counts,
        'error_count':len(j['errors']),'error_summary':list(dict.fromkeys(str(v) for v in j['errors'].values()))[:4]}

def disposition(did,jid,action):
    from . import draft,batch
    if action not in ('archive','restore','cancel'):raise ValueError('任务操作无效')
    with draft.WRITE_LOCK,Session.begin() as s:
        job=require(s,BatchJob,jid)
        if job.dataset_id!=did:raise ValueError('任务不属于数据集')
        if job.status in ('running','queued','pausing','cancelling'):raise ValueError('请先停止任务，等待写入完成再归档')
        item=s.get(WorkbenchMeta,'job:'+jid)
        if not item:item=WorkbenchMeta(id='job:'+jid,dataset_id=did,kind='job');s.add(item)
        item.data={'archived':action=='archive','action':action}
        if action=='cancel':job.status='cancelled'
        draft.emit(s,did,'job',{**obj(job),'archived':action=='archive'});audit(s,did,'task_'+action,{'job_id':jid})
        return {'id':jid,'archived':action=='archive','status':job.status}

def detail(jid):
    with Session() as s:
        job=require(s,BatchJob,jid);result=obj(job)
        result['attempts']=[obj(a) for a in s.scalars(select(ModelAttempt).where(ModelAttempt.job_id==jid).order_by(ModelAttempt.created_at.desc()).limit(50))]
        result['repairable']=[]
        for token in set(job.spec.get('expected',{}))-set(job.completed):
            if ':' not in token:continue
            pid,cid=token.split(':');product=s.get(Product,pid);col=s.get(DraftColumn,cid)
            if not product or not col:continue
            cell=s.scalar(select(Cell).where(Cell.product_id==pid,Cell.column_id==cid))
            eligible=owns(s,job,token) and not (cell and (cell.manual or cell.status=='ready'))
            result['repairable'].append({'token':token,'product_id':pid,'column_id':cid,'row':product.row,'anchor':product.anchor,'identity_source':product.identity_source,'column':col.definition['name'],'error':job.errors.get(token,'尚未完成'),'eligible':eligible,'reason':'' if eligible else '已有新任务、成功结果或人工修正，默认保护'})
        return result

def create_repair_scope(did,job_ids=None,tokens=None):
    job_ids=list(dict.fromkeys(job_ids or []));requested=set(tokens or [])
    if not job_ids:raise ValueError('请选择至少一个失败任务')
    rows=[];all_rows=[];infos=[]
    for jid in job_ids:
        info=detail(jid)
        infos.append(info)
        if info['dataset_id']!=did:raise ValueError('任务不属于数据集')
        if info['status'] in ('running','queued','pausing','cancelling'):raise ValueError('请先暂停执行中的任务')
        all_rows.extend(r|{'job_id':jid} for r in info['repairable'])
        rows.extend(r|{'job_id':jid} for r in info['repairable'] if r['eligible'])
    available={r['token']:r for r in rows}
    if requested and not requested<=available.keys():raise ValueError('修复范围已改变或包含受保护结果，请刷新任务详情')
    selected=[available[t] for t in requested] if requested else list(available.values())
    if not selected:raise ValueError('没有可修复的失败单元格')
    grouped={}
    for row in selected:grouped.setdefault(row['column_id'],[]).append(row['product_id'])
    targets=[{'column_id':cid,'product_ids':list(dict.fromkeys(ids))} for cid,ids in grouped.items()]
    with Session() as s:
        guard={}
        summary=[]
        for cid,ids in grouped.items():
            col=require(s,DraftColumn,cid);chosen={r['token'] for r in selected if r['column_id']==cid}
            completed={token for info in infos for token in info.get('completed',[]) if token.endswith(':'+cid)}
            protected={r['token'] for r in all_rows if r['column_id']==cid and not r['eligible']}
            for token in chosen:
                pid,_=token.split(':');cell=s.scalar(select(Cell).where(Cell.product_id==pid,Cell.column_id==cid));owner=s.get(WorkbenchMeta,'owner:'+token)
                guard[token]={'owner_job_id':owner.data.get('job_id') if owner else None,'edit':cell.edit if cell else 0,
                              'revision':cell.revision if cell else col.revision,'status':cell.status if cell else 'pending','manual':cell.manual if cell else None}
            method=col.definition.get('method','model')
            summary.append({'column_id':cid,'name':col.definition['name'],'failed':len(chosen),'rerun':len(chosen),
                            'preserved_success':len(completed),'protected':len(protected),
                            'estimated_requests':0 if method in ('text_rule','mapping','bins','attribute') else (len(set(ids))+4)//5,
                            'human_values_protected':True})
    ident='repair:'+uid();data={'id':ident,'job_ids':job_ids,'tokens':[r['token'] for r in selected],'targets':targets,'items':selected,'guard':guard,'summary':summary,'cell_count':len(selected),'column_count':len(targets),'created_at':now()}
    with Session.begin() as s:s.add(WorkbenchMeta(id=ident,dataset_id=did,kind='repair_scope',data=data))
    return data

def get_repair_scope(did,ident):
    with Session() as s:
        item=require(s,WorkbenchMeta,ident)
        if item.dataset_id!=did or item.kind!='repair_scope':raise ValueError('修复范围不属于当前数据集')
        return item.data

def list_repair_scopes(did,limit=10):
    """A saved repair scope must be discoverable without already knowing its ID."""
    with Session() as s:
        items=list(s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='repair_scope').order_by(WorkbenchMeta.updated_at.desc()).limit(max(1,min(limit,50)))))
    return [{'repair_scope_id':item.id,**{k:item.data.get(k) for k in ('cell_count','column_count','summary','created_at')}} for item in items]

def validate_repair_scope(s,did,data):
    if data.get('dataset_id') not in (None,did):raise ValueError('修复范围不属于当前数据集')
    for token,expected in data.get('guard',{}).items():
        pid,cid=token.split(':');cell=s.scalar(select(Cell).where(Cell.product_id==pid,Cell.column_id==cid));owner=s.get(WorkbenchMeta,'owner:'+token)
        current={'owner_job_id':owner.data.get('job_id') if owner else None,'edit':cell.edit if cell else 0,
                 'revision':cell.revision if cell else require(s,DraftColumn,cid).revision,'status':cell.status if cell else 'pending','manual':cell.manual if cell else None}
        if current!=expected:raise ValueError('修复范围已变化，请重新读取失败项并生成提案')

def repair(did,jid=None,tokens=None,definitions=None,refresh_images=False,repair_scope_id=None):
    """Freeze sparse failing cells per column; only the ordinary human proposal can execute."""
    from . import draft
    scope=get_repair_scope(did,repair_scope_id) if repair_scope_id else create_repair_scope(did,[jid],tokens)
    grouped={t['column_id']:t['product_ids'] for t in scope['targets']}
    definitions=definitions or {}
    if any(cid not in grouped for cid in definitions):raise ValueError('修订列不在修复范围内')
    changes=[]
    for cid,ids in grouped.items():
        if cid in definitions:changes.append({'kind':'revise','column_id':cid,'definition':definitions[cid]})
    changes.append({'kind':'execute','repair_scope_id':scope['id'],'repair_summary':scope.get('summary',[]),'overwrite':False,'refresh_images':bool(refresh_images)})
    return draft.propose(did,'局部修复：'+str(scope['cell_count'])+' 个单元格',changes)

def reject_candidate(s,jid,model,data):s.add(ModelAttempt(job_id=jid,model=model,data=data))
