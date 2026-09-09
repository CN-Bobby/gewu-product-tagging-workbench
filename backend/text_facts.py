"""Resumable, text-only information groundwork; never creates label columns."""
import copy
import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from .db import *
from . import draft,tagging_rules

def source_for(p):
    # Preserve both detail columns if a workbook contains both, without guessing priority.
    from .field_catalog import sources
    source={k:p.fields.get(k) for k in ('sku','parameters','title')}
    source.update(sources(p))
    for h in ('详细信息','详细参数'):
        if h in p.raw:source[h]=p.raw[h]
    return source

def fingerprint(source,model,rules,prefix=None,identity=None):
    from .execution_contract import TEXT_PREFIX,model_identity
    prefix=TEXT_PREFIX if prefix is None else prefix
    identity=model_identity(model) if identity is None else identity
    return hashlib.sha256(json.dumps([source,model,rules['sha256'],prefix,identity],ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def emit_record(s,did,r):
    s.flush();draft.emit(s,did,'text_record',obj(r))

def prepare(did,model=None):
    from .harness import DEFAULT_MODEL
    from .model_settings import defaults
    model=model or defaults()[0];rules=tagging_rules.freeze()
    with draft.WRITE_LOCK,Session.begin() as s:
        require(s,Dataset,did)
        existing=s.scalars(select(BatchJob).where(BatchJob.dataset_id==did,BatchJob.status.in_(['queued','running','pausing','cancelling']))).all()
        existing=next((j for j in existing if j.spec.get('kind')=='text_facts'),None)
        if existing:return obj(existing)
        inputs={};expected={};jid=uid()
        for p in s.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)):
            src=source_for(p);fp=fingerprint(src,model,rules)
            r=s.scalar(select(TextRecord).where(TextRecord.product_id==p.id))
            if r and r.fingerprint==fp and r.status=='ready':continue
            if not r:r=TextRecord(product_id=p.id);s.add(r)
            r.fingerprint=fp;r.source=src;r.status='pending';r.error=None;r.content={}
            r.provenance={'model':model,'skill_version':rules['version'],'skill_sha256':rules['sha256'],'pending_job_id':jid}
            inputs[p.id]=src;expected[p.id]={'fingerprint':fp};emit_record(s,did,r)
        job=BatchJob(id=jid,dataset_id=did,status='queued' if inputs else 'completed',spec={'kind':'text_facts','columns':[],'expected':expected,'inputs':inputs,'product_ids':list(inputs),'text_model':model,'rules':rules},metrics={})
        s.add(job);s.flush();draft.emit(s,did,'job',obj(job));return obj(job)

def records(did,offset=0,limit=50):
    with Session() as s:
        require(s,Dataset,did)
        return [obj(r) for r in s.scalars(select(TextRecord).join(Product,TextRecord.product_id==Product.id).where(Product.dataset_id==did).order_by(Product.row).offset(max(0,offset)).limit(max(1,min(limit,100))))]

def relevant(record,definition):
    target=definition['name']+' '+definition['definition']+' '+definition.get('judgment',{}).get('target','')
    facts=[]
    for f in record['content']['facts']:
        name=f['attribute']
        if name in target or any(name[i:i+2] in target for i in range(len(name)-1)):
            facts.append(f)
    fields={e['field'] for f in facts for e in f.get('evidence',[])}
    return {**record,'content':{'facts':facts},'source':{k:v for k,v in record['source'].items() if k in fields}}

_FIELD_ALIASES={
    '参数':['parameters','详细参数','详细信息'],
    'parameters':['parameters','详细参数','详细信息'],
    '详细参数':['详细参数','parameters','详细信息'],
    '详细信息':['详细信息','parameters','详细参数'],
    '标题':['title','商品标题'],
    'title':['title','商品标题'],
    '商品标题':['商品标题','title'],
    'sku':['sku','SKU'],
    'SKU':['SKU','sku'],
}

def _unwrapped_field(field):
    if not isinstance(field,str):return field
    field=field.strip()
    match=re.fullmatch(r"(?:text|source)\s*\[\s*(['\"])(.+?)\1\s*\]",field,re.I)
    return match.group(2).strip() if match else field

def _source_match(source,field,quote):
    """Return an exact source slice. Never manufactures or fuzzy-matches evidence."""
    if not isinstance(quote,str) or not quote.strip():return None
    quote=quote.strip();requested=_unwrapped_field(field)
    keys=[]
    def add(key):
        if key in source and key not in keys:keys.append(key)
    add(requested)
    if isinstance(requested,str):
        for key in source:
            if key.casefold()==requested.casefold():add(key)
        for key in _FIELD_ALIASES.get(requested,[]):add(key)
    for key in source:add(key)
    for key in keys:
        text=str(source.get(key) or '')
        index=text.find(quote)
        mode='exact'
        if index<0:
            match=re.search(re.escape(quote),text,re.I)
            if not match:continue
            index=match.start();mode='case_insensitive'
        return {'field':key,'quote':text[index:index+len(quote)],'mode':mode,'requested_field':field}
    return None

def validate(value,source,salvage=False):
    if not isinstance(value,dict):raise ValueError('文字底稿商品结果必须是对象')
    facts=value.get('facts')
    if not isinstance(facts,list):raise ValueError('文字底稿缺少 facts 数组')
    facts=copy.deepcopy(facts);accepted=[];warnings=[]
    for fact in facts:
      try:
        if not isinstance(fact,dict) or not isinstance(fact.get('attribute'),str) or not fact['attribute'].strip():raise ValueError('底稿属性缺失')
        if not re.search(r'[\u4e00-\u9fff]',fact['attribute']):raise ValueError('底稿属性名称需要中文')
        if fact.get('state') not in ('observed','conflict','unknown'):raise ValueError('底稿状态无效')
        if 'value' not in fact or (fact['value'] is not None and not isinstance(fact['value'],(str,int,float,list))):raise ValueError('底稿值类型无效')
        if fact['state']!='observed' and fact['value'] is not None:
            if not salvage:raise ValueError('冲突或未知不得输出确定值')
            warnings.append({'code':'discarded_nondefinitive_value','attribute':fact['attribute'],'message':'未知或冲突状态中的确定值已清空'})
            fact['discarded_value']=fact['value'];fact['value']=None
        evidence=fact.get('evidence')
        if not isinstance(evidence,list) or (fact['state'] in ('observed','conflict') and not evidence):raise ValueError('缺少原文证据')
        normalized=[]
        for e in evidence:
            located=_source_match(source,e.get('field') if isinstance(e,dict) else None,e.get('quote') if isinstance(e,dict) else None)
            if not located:
                if not salvage:raise ValueError('底稿证据无法定位到原文')
                warnings.append({'code':'discarded_evidence','attribute':fact['attribute'],'message':'无法定位到原文的证据项已丢弃'})
                continue
            item={'field':located['field'],'quote':located['quote']}
            requested=_unwrapped_field(located['requested_field'])
            if located['field']!=located['requested_field'] or located['mode']!='exact':
                item['original_field']=located['requested_field'];item['normalization']=located['mode'] if located['field']==requested else 'field_and_'+located['mode']
                warnings.append({'code':'normalized_evidence','attribute':fact['attribute'],'message':'证据字段或大小写已按原文规范化'})
            if item not in normalized:normalized.append(item)
        fact['evidence']=normalized
        if fact['state'] in ('observed','conflict') and not normalized:raise ValueError('底稿证据无法定位到原文')
        if fact['state']!='observed' and not fact.get('reason'):raise ValueError('未知或冲突需要原因')
        if fact.get('reason') and not re.search(r'[\u4e00-\u9fff]',fact['reason']):raise ValueError('底稿原因需要中文')
        identifier=any(k in fact['attribute'] for k in ('品牌','型号','SKU','ASIN','编号','原始标题','原文'))
        vals=fact['value'] if isinstance(fact['value'],list) else [fact['value']]
        if not identifier:
            for v in vals:
                if isinstance(v,str) and re.search(r'[A-Za-z]{3,}',v) and not re.search(r'[\u4e00-\u9fff]',v) and not re.fullmatch(r'[A-Z0-9 _./+\-]+',v):raise ValueError('底稿标准值需要中文，歧义款名应保留原文并标记未知')
        accepted.append(fact)
      except (ValueError,TypeError,AttributeError) as error:
        if not salvage:raise
        warnings.append({'code':'discarded_fact','attribute':fact.get('attribute') if isinstance(fact,dict) else None,'message':str(error)})
    # An explicit conflict invalidates a competing definitive value of that same attribute.
    # Keep all underlying observations, so no source silently becomes authoritative.
    merged=[];handled=set()
    for fact in accepted:
        name=fact['attribute'];related=[f for f in accepted if f['attribute']==name]
        if any(f['state']=='conflict' for f in related):
            if name in handled:continue
            handled.add(name);evidence=[]
            for f in related:
                for e in f['evidence']:
                    if e not in evidence:evidence.append(e)
            merged.append({'attribute':name,'value':None,'state':'conflict','raw_value':[f.get('raw_value') for f in related],
                'reason':'；'.join(dict.fromkeys(f.get('reason','') for f in related if f.get('reason'))),'evidence':evidence,'observations':related})
        else:merged.append(fact)
    result={'facts':merged}
    if warnings:result['validation_warnings']=warnings
    return result

def validate_resume(jid):
    with Session() as s:
        job=require(s,BatchJob,jid)
        for pid in set(job.spec['expected'])-set(job.completed):
            r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid));p=require(s,Product,pid)
            if not r or r.provenance.get('pending_job_id',jid)!=jid or r.fingerprint!=job.spec['expected'][pid]['fingerprint']:raise ValueError('此底稿任务已被新的提取替代，请继续最新任务')
            if r.source!=source_for(p):raise ValueError('原文已变化，请重新提取文字底稿')

def execute(jid):
    from . import batch
    validate_resume(jid)
    start=time.perf_counter();cpu=time.thread_time();mlock=threading.Lock()
    metrics={'requests':0,'vision_requests':0,'cache_hits':0,'save_seconds':0,'model_seconds':0,'application_cpu_seconds':0}
    stop=batch.active[jid]
    with draft.WRITE_LOCK,Session.begin() as s:
        job=require(s,BatchJob,jid);spec=job.spec;did=job.dataset_id;done=set(job.completed)
        metrics.update(job.metrics);metrics['job_id']=jid;metrics['execution_attempts']=metrics.get('execution_attempts',0)+1;prior_wall=metrics.get('wall_seconds',0)
        from .build_info import build_info
        metrics['execution_build']=build_info()
        if not stop.is_set():job.status='running'
        draft.emit(s,did,'job',obj(job))
    from .execution_contract import require_contract,check_models
    contract=require_contract(spec)
    check_models(spec)
    def save(pid,value,cached=False,error=None):
        ts=time.perf_counter()
        with draft.WRITE_LOCK,Session.begin() as s:
            j=require(s,BatchJob,jid);r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid));p=require(s,Product,pid)
            expected=spec['expected'][pid]['fingerprint']
            if r.provenance.get('pending_job_id',jid)!=jid or r.fingerprint!=expected or fingerprint(source_for(p),spec['text_model'],spec['rules'],contract['text_prefix'],contract['models'].get('text_model'))!=expected:
                raise ValueError('原文已改变，请重新提取')
            errors=dict(j.errors)
            if error:r.status='failed';r.error=str(error)[:400];errors[pid]=r.error
            else:
                r.content=value;r.status='ready';r.error=None;r.provenance={**r.provenance,'job_id':jid,'cached':cached,'execution_build':build_info()}
                errors.pop(pid,None);j.completed=list(set(j.completed+[pid]))
                if not cached:
                    c=s.scalar(select(TextCache).where(TextCache.fingerprint==expected))
                    if not c:s.add(TextCache(fingerprint=expected,content=value))
                with mlock:metrics['cache_hits']+=int(cached)
            j.errors=errors;j.metrics=dict(metrics);emit_record(s,did,r);draft.emit(s,did,'job',obj(j))
        with mlock:metrics['save_seconds']+=time.perf_counter()-ts
    def group(ids):
        if stop.is_set():return
        pending=[]
        for pid in ids:
            with Session() as s:c=s.scalar(select(TextCache).where(TextCache.fingerprint==spec['expected'][pid]['fingerprint']));cached=c.content if c else None
            if cached is not None:save(pid,cached,True)
            elif not any(spec['inputs'][pid].values()):save(pid,{'facts':[{'attribute':'原始文字','value':None,'state':'unknown','reason':'三列均缺失或为空','evidence':[]}]})
            else:pending.append(pid)
        if not pending or stop.is_set():return
        inputs=[{'product_id':pid,'text':spec['inputs'][pid]} for pid in pending]
        if len(json.dumps(inputs,ensure_ascii=False))>24000 and len(pending)>1:
            n=len(pending)//2;group(pending[:n]);group(pending[n:]);return
        validation_errors={}
        try:
            result=batch.request_json(spec['text_model'],[{'role':'system','content':tagging_rules.prompt(spec['rules'],['core','text','normalization'])+contract['text_prefix']}, {'role':'user','content':json.dumps(inputs,ensure_ascii=False)}],metrics,mlock)
            for value in batch.result_items(result):
                pid=value.get('product_id')
                if pid not in pending:continue
                try:validated=validate(value,spec['inputs'][pid],salvage=True)
                except (ValueError,TypeError,AttributeError) as e:
                    validation_errors[pid]=str(e)
                    with draft.WRITE_LOCK,Session.begin() as s:
                        r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid))
                        if r.provenance.get('pending_job_id',jid)==jid and r.fingerprint==spec['expected'][pid]['fingerprint']:
                            r.content={'rejected_candidate':value,'validation_error':str(e)}
                    continue
                save(pid,validated);pending.remove(pid)
            if pending:raise ValueError('文字底稿未通过校验：'+json.dumps(validation_errors,ensure_ascii=False) if validation_errors else '文字底稿缺少商品结果')
        except Exception as e:
            if isinstance(e,(ValueError,KeyError,TypeError)) and len(pending)>1:
                n=len(pending)//2;group(pending[:n]);group(pending[n:])
            elif isinstance(e,(ValueError,KeyError,TypeError)) and len(inputs)>1 and pending:
                group(pending)
            else:
                for pid in pending:save(pid,None,error=e)
    def timed(ids):
        t=time.thread_time()
        try:group(ids)
        finally:
            with mlock:metrics['application_cpu_seconds']+=time.thread_time()-t
    try:
        ids=[pid for pid in spec['product_ids'] if pid not in done]
        with ThreadPoolExecutor(max_workers=3) as pool:
            fs=[pool.submit(timed,ids[i:i+5]) for i in range(0,len(ids),5)]
            for f in fs:f.result()
        with draft.WRITE_LOCK,Session.begin() as s:
            j=require(s,BatchJob,jid)
            j.status='cancelled' if j.status=='cancelling' else 'completed' if len(j.completed)==len(spec['expected']) else 'paused' if stop.is_set() else 'partial'
            metrics['wall_seconds']=prior_wall+time.perf_counter()-start;metrics['last_attempt_seconds']=time.perf_counter()-start;metrics['application_cpu_seconds']+=time.thread_time()-cpu;j.metrics=metrics;draft.emit(s,did,'job',obj(j))
    finally:
        with batch.guard:batch.active.pop(jid,None)

def for_batch(pid,stop,allow_raw=False,definition=None):
    """Old datasets have no records; they retain their authorized raw-text path."""
    while not stop.is_set():
        with Session() as s:
            r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid))
            if not r:return None
            p=require(s,Product,pid)
            if source_for(p)!=r.source:
                if allow_raw:return None
                raise ValueError('文字底稿原文已变化，请重新提取')
            if r.status=='ready':return {'content':r.content,'source':r.source,'provenance':r.provenance,'fingerprint':r.fingerprint}
            if r.status in ('failed','stale'):
                if allow_raw:return None
                raise ValueError('文字底稿提取失败：'+str(r.error))
            active=s.scalars(select(BatchJob).where(BatchJob.dataset_id==p.dataset_id,BatchJob.status.in_(['queued','running']))).all()
            if not any(j.spec.get('kind')=='text_facts' and pid in j.spec['expected'] for j in active):raise ValueError('文字底稿已暂停或中断，请先继续提取')
        stop.wait(.2)
    raise ValueError('等待文字底稿时任务已暂停')
