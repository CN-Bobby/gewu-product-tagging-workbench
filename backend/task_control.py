"""Task lifecycle and writes are application-owned, independent of agent narration."""
from sqlalchemy import select
from .db import *

def claim(s,did,token,jid):
    key='owner:'+token;owner=s.get(WorkbenchMeta,key)
    if not owner:owner=WorkbenchMeta(id=key,dataset_id=did,kind='owner');s.add(owner)
    owner.data={'job_id':jid};owner.updated_at=now()

def owns(s,job,token):
    owner=s.get(WorkbenchMeta,'owner:'+token)
    return not owner or owner.data.get('job_id')==job.id

def brief(job):
    j=obj(job) if not isinstance(job,dict) else job;spec=j['spec']
    return {k:v for k,v in j.items() if k not in ('spec','errors','completed')}|{
        'spec':{k:spec.get(k) for k in ('kind','sample','text_model','vision_model')}|{'product_count':len(spec.get('product_ids',[])),'columns':[{'id':c['id'],'revision':c['revision'],'definition':{'name':c['definition']['name']}} for c in spec.get('columns',[])]},
        'completed_count':len(j['completed']),'expected_count':len(spec.get('expected',{})),
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
            result['repairable'].append({'token':token,'product_id':pid,'column_id':cid,'row':product.row,'anchor':product.anchor,'column':col.definition['name'],'error':job.errors.get(token,'尚未完成'),'eligible':eligible,'reason':'' if eligible else '已有新任务、成功结果或人工修正，默认保护'})
        return result

def repair(did,jid,tokens,definitions=None,refresh_images=False):
    """Freeze sparse failing cells per column; only the ordinary human proposal can execute."""
    from . import draft
    info=detail(jid)
    if info['dataset_id']!=did:raise ValueError('任务不属于数据集')
    if info['status'] in ('running','queued','pausing','cancelling'):raise ValueError('请先暂停任务再修复')
    if not isinstance(tokens,list) or not tokens:raise ValueError('请选择需要修复的失败单元格')
    eligible={r['token']:r for r in info['repairable'] if r['eligible']}
    if any(t not in eligible for t in tokens):raise ValueError('修复范围已改变或包含受保护结果，请刷新任务详情')
    grouped={}
    for t in dict.fromkeys(tokens):
        r=eligible[t];grouped.setdefault(r['column_id'],[]).append(r['product_id'])
    definitions=definitions or {}
    if any(cid not in grouped for cid in definitions):raise ValueError('修订列不在修复范围内')
    changes=[]
    for cid,ids in grouped.items():
        if cid in definitions:changes.append({'kind':'revise','column_id':cid,'definition':definitions[cid]})
        changes.append({'kind':'execute','column_ids':[cid],'product_ids':ids,'overwrite':False,'refresh_images':bool(refresh_images)})
    return draft.propose(did,'局部修复：'+str(len(tokens))+' 个单元格',changes)

def reject_candidate(s,jid,model,data):s.add(ModelAttempt(job_id=jid,model=model,data=data))
