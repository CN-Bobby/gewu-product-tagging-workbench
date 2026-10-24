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
        if any(j.spec.get('kind')=='image_download' for j in existing):
            raise ValueError('图片下载任务仍在运行，请等待结束后再提取文字属性')
        existing=next((j for j in existing if j.spec.get('kind')=='text_facts'),None)
        if existing:return obj(existing)
        inputs={};expected={};jid=uid();image_assets={}
        for p in s.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)):
            src=source_for(p);fp=fingerprint(src,model,rules)
            r=s.scalar(select(TextRecord).where(TextRecord.product_id==p.id))
            has_attribute_input=any(str(src.get(k) or '').strip() for k in ('sku','parameters'))
            attributes_complete=(r.content or {}).get('source_attributes_complete',False) if r else False
            if r and r.fingerprint==fp and r.status=='ready' and (attributes_complete or not has_attribute_input):continue
            if not r:r=TextRecord(product_id=p.id);s.add(r)
            r.fingerprint=fp;r.source=src;r.status='pending';r.error=None;r.content={}
            r.provenance={'model':model,'skill_version':rules['version'],'skill_sha256':rules['sha256'],'pending_job_id':jid}
            inputs[p.id]=src;expected[p.id]={'fingerprint':fp};emit_record(s,did,r)
        for asset in s.scalars(select(Asset).join(Product,Asset.product_id==Product.id)
                               .where(Product.dataset_id==did,Asset.url.is_not(None),Asset.path.is_(None))):
            image_assets[asset.id]={'product_id':asset.product_id,'url':asset.url}
        job=BatchJob(id=jid,dataset_id=did,status='queued' if inputs or image_assets else 'completed',
                     spec={'kind':'text_facts','columns':[],'expected':expected,'inputs':inputs,
                           'product_ids':list(inputs),'image_assets':image_assets,'text_model':model,'rules':rules},
                     metrics={'attributes_expected':len(inputs),'attributes_completed':0,'images_expected':len(image_assets),
                              'images_completed':0,'images_failed':0,'images_pending':len(image_assets)})
        s.add(job);s.flush();draft.emit(s,did,'job',obj(job));return obj(job)


def prepare_images(did):
    """Queue only missing workbook URL images; never schedule text/model work."""
    with draft.WRITE_LOCK,Session.begin() as s:
        require(s,Dataset,did)
        active=s.scalars(select(BatchJob).where(
            BatchJob.dataset_id==did,
            BatchJob.status.in_(['queued','running','pausing','cancelling']))).all()
        for job in active:
            if job.spec.get('kind')=='image_download':
                return obj(job)
            if job.spec.get('kind')=='text_facts' and job.spec.get('image_assets'):
                raise ValueError('导入整理任务仍在处理图片，请等待任务结束后再下载缺失图片')
        assets=s.scalars(select(Asset).join(Product,Asset.product_id==Product.id)
                         .where(Product.dataset_id==did,Asset.url.is_not(None))).all()
        pending={asset.id:{'product_id':asset.product_id,'url':asset.url}
                 for asset in assets if not asset.path or not Path(asset.path).is_file()}
        if not pending:
            completed=s.scalars(select(BatchJob).where(BatchJob.dataset_id==did,BatchJob.status=='completed')
                                .order_by(BatchJob.created_at.desc())).all()
            followup=next((job.id for job in completed if job.spec.get('kind')=='image_download'),None)
            _settle_old_image_failures(s,did,followup)
            return {'id':None,'status':'completed','spec':{'kind':'image_download'},
                    'metrics':{'images_expected':0,'images_completed':0,'images_failed':0}}
        job=BatchJob(id=uid(),dataset_id=did,status='queued',
                     spec={'kind':'image_download','columns':[],'expected':{},'inputs':{},
                           'product_ids':[],'image_assets':pending},
                     metrics={'images_expected':len(pending),'images_completed':0,
                              'images_failed':0,'images_pending':len(pending),'requests':0})
        s.add(job);s.flush();draft.emit(s,did,'job',obj(job));return obj(job)


def _settle_old_image_failures(s,did,followup_job_id):
    """Close historical image-only failures once every referenced file is present.

    Keep the original errors and counts as an audit trail; the old run itself did fail.
    """
    prior=s.scalars(select(BatchJob).where(BatchJob.dataset_id==did,BatchJob.status=='partial')).all()
    for job in prior:
        spec=job.spec or {};assets=spec.get('image_assets') or {};errors=job.errors or {}
        if spec.get('kind')!='text_facts' or not assets or not errors:
            continue
        if not all(key.startswith('image:') for key in errors):
            continue
        if not set(spec.get('expected') or {})<=set(job.completed or []):
            continue
        if not all((asset:=s.get(Asset,asset_id)) and asset.path and Path(asset.path).is_file()
                   for asset_id in assets):
            continue
        job.status='resolved'
        job.metrics={**(job.metrics or {}),'resolved_by_image_job_id':followup_job_id,
                     'resolution':'后续图片下载已补齐；保留原任务失败记录'}
        draft.emit(s,did,'job',obj(job))

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

def validate_source_attributes(value,source,*,salvage=False):
    """Validate exact SKU/parameter key-value evidence returned by the model."""
    items=value.get('source_attributes',[])
    warnings=[]
    if not isinstance(items,list):
        if not salvage:raise ValueError('source_attributes 必须是数组')
        return [],[{'code':'invalid_source_attributes','message':'属性拆分结果格式无效'}]
    accepted=[]
    for item in items:
        try:
            if not isinstance(item,dict):raise ValueError('属性项必须是对象')
            source_name=item.get('source')
            if source_name not in ('sku','parameters'):raise ValueError('属性来源必须是 sku 或 parameters')
            attribute=item.get('attribute')
            if not isinstance(attribute,str) or not attribute.strip():raise ValueError('属性名为空')
            attribute=' '.join(attribute.strip().split())
            raw_value=item.get('value')
            if raw_value is None or isinstance(raw_value,(dict,list,bool)):
                raise ValueError('属性值必须是原文中的文本或数字')
            raw_value=str(raw_value).strip()
            if not raw_value:raise ValueError('属性值为空')
            evidence=item.get('evidence')
            matched=_source_match(source,source_name,evidence)
            if not matched or matched['field'] not in (source_name, 'SKU' if source_name=='sku' else '详细参数', '详细信息'):
                raise ValueError('属性证据无法定位到对应来源原文')
            original=source.get(matched['field'])
            evidence_text=str(original or '')[original.find(evidence):original.find(evidence)+len(evidence)] if isinstance(original,str) and evidence in original else ''
            if not evidence_text or raw_value not in evidence_text:
                raise ValueError('属性值不是证据片段中的原文')
            accepted.append({'source':source_name,'attribute':attribute,'value':raw_value,'evidence':evidence_text})
        except (ValueError,TypeError,AttributeError) as error:
            if not salvage:raise
            warnings.append({'code':'discarded_source_attribute','message':str(error)})
    return accepted,warnings

def source_attribute_columns(attributes):
    """Map model-extracted values to stable source-qualified raw columns."""
    groups={};labels={};warnings=[]
    for item in attributes:
        source=item['source'];display_attribute=' '.join(item['attribute'].split())
        key=(source,display_attribute.casefold())
        labels.setdefault(key,display_attribute)
        groups.setdefault(key,set()).add(item['value'])
    columns={}
    for (source,key),values in groups.items():
        prefix='SKU' if source=='sku' else '详细参数'
        header=f'{prefix}-{labels[(source,key)]}属性'
        if len(values)==1:columns[header]=next(iter(values))
        else:
            columns[header]=None
            warnings.append({'code':'conflicting_source_attribute','source':source,'attribute':labels[(source,key)],'message':'同一行中识别到多个不同值，已留空供复核'})
    return columns,warnings

def validate(value,source,salvage=False,require_source_attributes=False):
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
    source_attributes_present='source_attributes' in value
    if require_source_attributes and not source_attributes_present:
        raise ValueError('文字底稿缺少 source_attributes，无法整理 SKU / 详细参数属性')
    source_attributes,attribute_warnings=validate_source_attributes(value,source,salvage=salvage)
    columns,column_warnings=source_attribute_columns(source_attributes)
    result={'facts':merged,'source_attributes':source_attributes,'source_attribute_columns':columns,
            'source_attributes_complete':source_attributes_present and not attribute_warnings and not column_warnings}
    if not source_attributes_present and any(str(source.get(k) or '').strip() for k in ('sku','parameters')):
        attribute_warnings.append({'code':'missing_source_attributes','message':'模型未返回 SKU / 详细参数结构化结果'})
    warnings.extend(attribute_warnings);warnings.extend(column_warnings)
    if warnings:result['validation_warnings']=warnings
    return result

def _write_source_attribute_columns(s,did,product,record):
    values=record.get('source_attribute_columns') or {}
    if not values:return False
    raw=dict(product.raw or {});raw.update(values);product.raw=raw
    dataset=require(s,Dataset,did);mapping=dict(dataset.mapping or {})
    base=mapping.get('__source_attribute_base_headers')
    if not isinstance(base,list):
        prior=set(mapping.get('__source_attribute_headers') or [])
        base=[header for header in (dataset.headers or []) if header not in prior]
    headers=set(mapping.get('__source_attribute_headers') or [])|set(values)
    ordered=sorted(headers,key=lambda name:(0 if name.startswith('SKU-') else 1,name.casefold()))
    mapping['__source_attribute_base_headers']=list(base)
    mapping['__source_attribute_headers']=ordered
    dataset.mapping=mapping
    dataset.headers=list(base)+ordered
    return True

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
    image_only=spec.get('kind')=='image_download'
    if not image_only:
        from .execution_contract import require_contract,check_models
        contract=require_contract(spec)
        check_models(spec)
    def save(pid,value,cached=False,error=None):
        ts=time.perf_counter()
        with draft.WRITE_LOCK,Session.begin() as s:
            j=require(s,BatchJob,jid);r=s.scalar(select(TextRecord).where(TextRecord.product_id==pid));p=require(s,Product,pid)
            dataset=require(s,Dataset,did)
            expected=spec['expected'][pid]['fingerprint']
            if r.provenance.get('pending_job_id',jid)!=jid or r.fingerprint!=expected or fingerprint(source_for(p),spec['text_model'],spec['rules'],contract['text_prefix'],contract['models'].get('text_model'))!=expected:
                raise ValueError('原文已改变，请重新提取')
            errors=dict(j.errors)
            if error:r.status='failed';r.error=str(error)[:400];errors[pid]=r.error
            else:
                r.content=value;r.status='ready';r.error=None;r.provenance={**r.provenance,'job_id':jid,'cached':cached,'execution_build':build_info()}
                has_attribute_input=any(str(spec['inputs'][pid].get(k) or '').strip() for k in ('sku','parameters'))
                complete=bool(value.get('source_attributes_complete')) or not has_attribute_input
                changed=_write_source_attribute_columns(s,did,p,value)
                if complete:
                    errors.pop(pid,None)
                    if pid not in j.completed:
                        j.completed=list(j.completed)+[pid]
                        with mlock:metrics['attributes_completed']=metrics.get('attributes_completed',0)+1
                else:
                    j.completed=[item for item in j.completed if item!=pid]
                    errors[pid]='SKU / 详细参数属性解析不完整，可重试文字底稿提取'
                    with mlock:metrics['attributes_incomplete']=metrics.get('attributes_incomplete',0)+1
                if not cached and complete:
                    c=s.scalar(select(TextCache).where(TextCache.fingerprint==expected))
                    if not c:s.add(TextCache(fingerprint=expected,content=value))
                with mlock:metrics['cache_hits']+=int(cached)
                if changed:draft.emit(s,did,'reload',{'reason':'SKU / 详细参数属性已整理'})
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
            args=(spec['text_model'],[{'role':'system','content':tagging_rules.prompt(spec['rules'],['core','text','normalization'])+contract['text_prefix']}, {'role':'user','content':json.dumps(inputs,ensure_ascii=False)}],metrics,mlock)
            result=batch.request_json(*args,spec['text_credential_id']) if spec.get('text_credential_id') else batch.request_json(*args)
            for value in batch.result_items(result):
                pid=value.get('product_id')
                if pid not in pending:continue
                try:
                    has_attribute_input=any(str(spec['inputs'][pid].get(k) or '').strip() for k in ('sku','parameters'))
                    validated=validate(value,spec['inputs'][pid],salvage=True,
                                        require_source_attributes=has_attribute_input and 'source_attributes' in contract['text_prefix'])
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
    def download_asset(asset_id):
        from . import importer
        from .import_image_download import download_with_retry
        if stop.is_set():return
        with Session() as s:
            asset=s.get(Asset,asset_id)
            if not asset:return
            current_path=asset.path;url=asset.url;product_id=asset.product_id
            product=require(s,Product,product_id)
            row_label=f"第{product.row}行（{product.anchor}）"
        if current_path and Path(current_path).is_file():return
        result=download_with_retry(url or '',client=image_client)
        failure=result.error_message
        stored=None
        if result.ok:
            try:stored=importer.image_bytes(result.data)
            except Exception:failure='下载内容无法保存为有效图片'
        with draft.WRITE_LOCK,Session.begin() as s:
            job=require(s,BatchJob,jid);asset=s.get(Asset,asset_id);errors=dict(job.errors or {})
            error_key='image:'+asset_id;prior=errors.get(error_key)
            if stored:
                path,digest=stored
                asset.path=path;asset.sha256=digest
                errors.pop(error_key,None)
                draft.emit(s,did,'reload',{'reason':'图片 URL 已补全为本地图片'})
                with mlock:
                    metrics['images_completed']=metrics.get('images_completed',0)+1
                    if prior:metrics['images_failed']=max(0,metrics.get('images_failed',0)-1)
                    metrics['images_pending']=max(0,len(spec.get('image_assets',{}))-metrics['images_completed'])
            else:
                errors[error_key]=f"{row_label}：{failure or '图片下载失败'}"[:300]
                with mlock:
                    if not prior:metrics['images_failed']=metrics.get('images_failed',0)+1
                    metrics['images_pending']=max(0,len(spec.get('image_assets',{}))-metrics['images_completed'])
            job.errors=errors;job.metrics=dict(metrics);draft.emit(s,did,'job',obj(job))
    try:
        if not image_only:
            ids=[pid for pid in spec['product_ids'] if pid not in done]
            with ThreadPoolExecutor(max_workers=3) as pool:
                fs=[pool.submit(timed,ids[i:i+5]) for i in range(0,len(ids),5)]
                for f in fs:f.result()
        if not stop.is_set() and spec.get('image_assets'):
            from .import_image_download import image_client as open_image_client, IMAGE_DOWNLOAD_WORKERS
            with open_image_client() as image_client:
                with ThreadPoolExecutor(max_workers=IMAGE_DOWNLOAD_WORKERS) as pool:
                    fs=[pool.submit(download_asset,asset_id) for asset_id in spec['image_assets']]
                    for f in fs:f.result()
            from . import project_files
            with Session() as s:
                assets=s.execute(select(Asset).join(Product,Asset.product_id==Product.id)
                    .where(Asset.id.in_(list(spec['image_assets'])),Asset.path.is_not(None))).scalars().all()
                registrations=[(a.id,a.path,a.image_type,a.sha256,require(s,Product,a.product_id).dataset_id)
                               for a in assets]
                workspace=require(s,Dataset,did).workspace_id
            for asset_id,path,image_type,digest,dataset_id in registrations:
                try:project_files.register_file(workspace,'images',path,source_type='dataset_asset',source_id=asset_id,
                    name=Path(path).name,metadata={'dataset_id':dataset_id,'image_type':image_type,'sha256':digest})
                except Exception:pass
        with draft.WRITE_LOCK,Session.begin() as s:
            j=require(s,BatchJob,jid)
            image_errors=any(key.startswith('image:') for key in (j.errors or {}))
            all_images_saved=all((s.get(Asset,asset_id) and s.get(Asset,asset_id).path and Path(s.get(Asset,asset_id).path).is_file()) for asset_id in spec.get('image_assets',{}))
            text_complete=set(spec['expected'])<=set(j.completed)
            j.status='cancelled' if j.status=='cancelling' else 'paused' if stop.is_set() else 'completed' if text_complete and all_images_saved and not image_errors else 'partial'
            metrics['wall_seconds']=prior_wall+time.perf_counter()-start;metrics['last_attempt_seconds']=time.perf_counter()-start;metrics['application_cpu_seconds']+=time.thread_time()-cpu;j.metrics=metrics;draft.emit(s,did,'job',obj(j))
            if image_only and j.status=='completed':
                _settle_old_image_failures(s,did,jid)
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
