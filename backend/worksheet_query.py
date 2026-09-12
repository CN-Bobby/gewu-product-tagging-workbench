"""One condition contract for table filters and coordinator-scoped execution."""
import hashlib,json
from collections import Counter
from sqlalchemy import select
from .db import *
from . import draft

def representative_rows(data,cap=60):
    """Deterministic round-robin across broad category/text/asset strata, deduping images."""
    from . import tagging_rules
    buckets={};seen=set()
    for row in data['rows']:
        image_key=tuple(a.get('sha256') for a in row['assets'] if a.get('path')) or (row['id'],)
        if image_key in seen:continue
        seen.add(image_key)
        routed=tagging_rules.route({},'advisor',{'raw':row.get('raw',{}),'fields':row.get('fields',{})})
        category=(routed['categories'] or ['uncategorized'])[0]
        nonempty=sum(v not in (None,'') for v in row.get('raw',{}).values())
        asset_band='multi_image' if sum(bool(a.get('path')) for a in row['assets'])>1 else 'single_image'
        stratum=(category,asset_band,min(nonempty//3,3))
        buckets.setdefault(stratum,[]).append(row)
    chosen=[];groups=list(buckets.values());index=0
    while groups and len(chosen)<cap:
        next_groups=[]
        for group in groups:
            if index<len(group):chosen.append(group[index])
            if index+1<len(group):next_groups.append(group)
            if len(chosen)>=cap:break
        groups=next_groups;index+=1
    return chosen

def aggregate_visual_groups(groups,total,sample_ids):
    """Cross-group synthesis without another image call."""
    ready=[g for g in groups if g.get('status')=='ready']
    patterns=[];candidates=[];include=[];exclude=[];unresolved=[]
    for group in ready:
        patterns.extend(group.get('patterns',[]));candidates.extend(group.get('candidate_values',[]));unresolved.extend(group.get('unresolved',[]))
        for result in group.get('results',[]):
            candidates.extend(result.get('candidate_values',[]));unresolved.extend(result.get('unknown',[]))
        boundary=group.get('boundaries') or {};include.extend(boundary.get('include',[]));exclude.extend(boundary.get('exclude',[]))
    unique=lambda values:list(dict.fromkeys(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in values))
    decode=lambda values:[json.loads(x) for x in unique(values)]
    recent=[set(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in g.get('candidate_values',[])+g.get('patterns',[])) for g in ready[-2:]]
    stable=len(recent)>=2 and not (recent[-1]-recent[-2]) and not unresolved
    return {'scope':'dataset_standard','sample_ids':sample_ids,'sample_count':len(sample_ids),'total':total,
            'coverage':len(sample_ids)/total if total else 0,'candidate_values':decode(candidates),'patterns':decode(patterns),
            'boundaries':{'include':decode(include),'exclude':decode(exclude)},'unresolved':decode(unresolved),
            'stability':{'stable':stable,'rule':'连续两组无新增候选/模式且无未决项'},'confirmed':False,'revision':1}

def display(value):
    if value is None:return ''
    if value is True:return '✓'
    if value is False:return '❌'
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,separators=(',',':'))
    return str(value)

def value_for(row,cid):
    if cid=='asin':return row['anchor'],'ready'
    if cid=='image':return ('有图' if any(a.get('path') for a in row['assets']) else '无图'),'ready'
    if cid.startswith('raw:'):return row['raw'].get(cid[4:]),'ready'
    cell=row['cells'].get(cid,{})
    return cell.get('value'),cell.get('status','pending')

def valid_columns(data):return {'asin','image'}|{'raw:'+h for h in data['dataset']['headers']}|{c['id'] for c in data['columns']}

def matches(row,filters,search=''):
    if search and search.casefold() not in ' '.join([row['anchor'],*[display(v) for v in row['raw'].values()],*[display(c.get('value')) for c in row['cells'].values()]]).casefold():return False
    for f in filters:
        value,status=value_for(row,f['column_id']);text=display(value).casefold()
        if 'values' in f and not any(value==v and type(value)==type(v) for v in f['values']):return False
        if f.get('states') and status not in f['states']:return False
        keywords=f.get('keywords',[]);op=f.get('op','contains')
        if op=='empty' and value not in (None,''):return False
        if op=='not_empty' and value in (None,''):return False
        if keywords:
            def check(k):
                k=display(k).casefold()
                return text==k if op=='equals' else k not in text if op=='not_contains' else k in text
            if not (all(check(k) for k in keywords) if f.get('match')=='all' else any(check(k) for k in keywords)):return False
    return True

def validate(data,filters):
    if not isinstance(filters,list) or len(filters)>100:raise ValueError('筛选条件无效')
    for f in filters:
        if not isinstance(f,dict) or f.get('column_id') not in valid_columns(data):raise ValueError('筛选列不存在，请刷新条件')
        if f.get('op','contains') not in ('contains','equals','not_contains','empty','not_empty'):raise ValueError('筛选运算无效')
        for k in ('values','states','keywords'):
            if k in f and not isinstance(f[k],list):raise ValueError('筛选值需为数组')
    return filters

def query(did,filters=None,search='',facet=None,save=False,job_id=None):
    data=draft.sheet(did);filters=validate(data,filters or [])
    rows=[r for r in data['rows'] if matches(r,filters,search)]
    if job_id:
        with Session() as s:
            j=require(s,BatchJob,job_id)
            if j.dataset_id!=did:raise ValueError('任务不属于数据集')
            pending=set(j.spec['expected'])-set(j.completed);ids={t.split(':')[0] for t in pending}
        rows=[r for r in rows if r['id'] in ids]
    ids=[r['id'] for r in rows];result={'count':len(ids),'total':len(data['rows']),'product_ids':ids,'sample':[{'id':r['id'],'row':r['row'],'anchor':r['anchor']} for r in rows[:5]],'filters':filters,'search':search}
    if facet:
        if facet not in valid_columns(data):raise ValueError('筛选列不存在')
        candidates=[r for r in data['rows'] if matches(r,[f for f in filters if f['column_id']!=facet],search)]
        counts={};states=Counter()
        for r in candidates:
            v,st=value_for(r,facet);key=json.dumps(v,ensure_ascii=False,sort_keys=True);counts.setdefault(key,{'value':v,'count':0})['count']+=1;states[st]+=1
        result.update(values=list(counts.values()),states=dict(states),count_basis='应用其他列条件，不应用当前列条件')
    if save:
        ident=uid();fingerprint=hashlib.sha256(json.dumps([(r['id'],r['raw'],{cid:[c['value'],c['edit'],c['revision'],c['status']] for cid,c in r['cells'].items()}) for r in rows],sort_keys=True).encode()).hexdigest()
        with Session.begin() as s:s.add(WorkbenchMeta(id=ident,dataset_id=did,kind='scope',data={**result,'fingerprint':fingerprint,'job_id':job_id}))
        result['scope_id']=ident
    return result

def resolve_scope(s,did,ident):
    scope=require(s,WorkbenchMeta,ident)
    if scope.dataset_id!=did or scope.kind!='scope':raise ValueError('范围不属于当前数据集')
    return list(scope.data['product_ids'])

def profile(did):
    from . import field_catalog
    data=draft.sheet(did);result={'total':len(data['rows']),'basis':'全量原始字段统计；图片未自动全量识别','fields':[],'text_status':dict(Counter((r.get('text_record') or {}).get('status','not_started') for r in data['rows']))}
    for f in field_catalog.read(did)['fields']:
        values=[r['raw'].get(f['name']) for r in data['rows']];count=Counter(display(v) for v in values if v not in (None,''))
        result['fields'].append({**f,'distinct':len(count),'missing':sum(v in (None,'') for v in values),'top_values':count.most_common(12)})
    # Diverse, stable coverage across broad category/text/asset strata; no visual semantic claim.
    sample=[r['id'] for r in representative_rows(data,60)]
    result['representative_product_ids']=sample
    with Session() as s:
        latest=s.scalar(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='visual_overview').order_by(WorkbenchMeta.updated_at.desc(),WorkbenchMeta.id.desc()))
        if latest:
            value=latest.data
            result['latest_visual_overview']={'overview_id':latest.id,'goal':value.get('goal'),'sample_count':value.get('sample_count'),'successful':value.get('successful'),'input_policy':value.get('input_policy'),'basis':value.get('basis'),'visual_standard':value.get('visual_standard'),'full_scan_estimate':value.get('full_scan_estimate'),'groups':[{'patterns':g.get('patterns',[]),'candidate_values':g.get('candidate_values',[]),'boundaries':g.get('boundaries',{}),'unresolved':g.get('unresolved',[]),'coverage_note':g.get('coverage_note'),'results':g.get('results',[])} for g in value.get('groups',[]) if g.get('status')=='ready'],'created_at':value.get('created_at')}
    return result

def visual_overview(did,goal,limit=24,full_scan=False,confirmed_full_scan=False):
    """Adaptively compare diverse main-image samples and synthesize a dataset-level draft standard."""
    from concurrent.futures import ThreadPoolExecutor
    import threading,time
    from . import batch
    from .model_settings import defaults
    VISION_MODEL=defaults()[1]
    if not isinstance(goal,str) or not 3<=len(goal)<=2000:raise ValueError('请说明本次希望观察的结构或图案问题')
    if type(limit) is not int or not 1<=limit<=60:raise ValueError('代表样本上限为1–60')
    data=draft.sheet(did);summary=profile(did);all_rows=([r for r in data['rows'] if any(a.get('path') for a in r['assets'])] if full_scan else representative_rows(data,60))
    estimate_images=len(all_rows) if full_scan else min(limit,len(all_rows));estimate_requests=(estimate_images+5)//6
    estimated={'image_count':estimate_images,'group_size':6,'estimated_vision_requests':estimate_requests,
               'estimated_input_tokens':estimate_images*900+estimate_requests*1600,'estimated_output_tokens':estimate_requests*1200,
               'estimated_currency_cost':None,'estimate_basis':'每张图按约900输入token、每组提示约1600输入token、输出约1200token粗估；实际由供应商返回usage为准',
               'note':'当前模型配置未保存单价，不能伪造金额；填入模型价格后才能换算付费成本'}
    if full_scan and not confirmed_full_scan:
        return {'status':'confirmation_required','goal':goal,'total':len(data['rows']),'full_scan_estimate':estimated,'message':'全量看图尚未执行；请确认预计图片数与请求数后重试'}
    target=min(len(all_rows),len(all_rows) if full_scan else limit);rows=all_rows[:target];metrics={'stage':'calibration'};lock=threading.Lock();start=time.perf_counter()
    def observe(group):
        try:
            compared=batch.calibration_group(group,VISION_MODEL,goal,False,metrics,lock)
            by_id={item['product_id']:item for item in compared['products']}
            return [{'status':'ready','product_ids':[row['id'] for row in group],'patterns':compared['patterns'],'candidate_values':compared.get('candidate_values',[]),'boundaries':compared.get('boundaries',{'include':[],'exclude':[]}),'unresolved':compared.get('unresolved',[]),'new_type_count':compared.get('new_type_count',0),'coverage_note':compared['coverage_note'],'cached':compared['cached'],'results':[{'product_id':row['id'],'row':row['row'],'anchor':row['anchor'],**by_id[row['id']]} for row in group]}]
        except Exception as e:
            if len(group)>1:
                middle=len(group)//2
                return observe(group[:middle])+observe(group[middle:])
            return [{'status':'failed','product_ids':[group[0]['id']],'error':str(e)[:500]}]
    # User-specified limit is the hard cap. Observe 6–10 first, then expand while novelty/uncertainty remains.
    initial=min(target,10);observed=0;group_results=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        while observed<target:
            stage_end=initial if observed<initial else min(target,24 if observed<24 else 60 if not full_scan else target)
            stage=[rows[i:i+6] for i in range(observed,stage_end,6)]
            group_results.extend(item for result in pool.map(observe,stage) for item in result);observed=stage_end
            ready=[g for g in group_results if g['status']=='ready']
            recent=ready[-2:]
            novelty=any(g.get('new_type_count',0) or g.get('candidate_values') or g.get('unresolved') for g in recent)
            pattern_sets=[set(json.dumps(x,ensure_ascii=False,sort_keys=True) for x in g.get('patterns',[])) for g in recent]
            if len(pattern_sets)>=2 and pattern_sets[-1]-pattern_sets[-2]:novelty=True
            if not full_scan and observed>=initial and not novelty:break
            if not full_scan and observed>=24 and not any(g.get('unresolved') or g.get('new_type_count',0) for g in recent):break
    results=[item for group in group_results if group['status']=='ready' for item in group['results']]
    sample_ids=[r['product_id'] for r in results];standard=aggregate_visual_groups(group_results,summary['total'],sample_ids);standard.update(goal=goal,input_policy='single_main_image_v1',failed_product_ids=[pid for g in group_results if g['status']=='failed' for pid in g['product_ids']])
    metrics['wall_seconds']=time.perf_counter()-start;metrics['requested_limit']=limit;metrics['adaptive_stop_at']=len(sample_ids)
    value={'goal':goal,'total':summary['total'],'sample_count':len(sample_ids),'successful':len(results),'input_policy':'single_main_image_v1','group_size':6,'basis':'按大类、文字完整度和图片形态分层去重抽样，每组最多6件；结论只覆盖所列样本，不代表全量已看图','groups':group_results,'results':results,'visual_standard':standard,'full_scan':bool(full_scan),'full_scan_estimate':estimated,'metrics':metrics,'created_at':now()}
    with draft.WRITE_LOCK,Session.begin() as s:
        ident=uid();value['overview_id']=ident;record=WorkbenchMeta(id=ident,dataset_id=did,kind='visual_overview',data=value);s.add(record);audit(s,did,'visual_overview',{'overview_id':ident,'goal':goal,'sample_count':value['sample_count'],'successful':value['successful'],'input_policy':value['input_policy'],'full_scan':bool(full_scan)})
    return value
