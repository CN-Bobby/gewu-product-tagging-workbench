"""One condition contract for table filters and coordinator-scoped execution."""
import hashlib,json
from collections import Counter
from sqlalchemy import select
from .db import *
from . import draft

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
    # Stable representative coverage across distinct images; no claim of observed visual semantics.
    seen=set();sample=[]
    for r in data['rows']:
        key=tuple(a.get('sha256') for a in r['assets'] if a.get('path')) or (r['id'],)
        if key not in seen:sample.append(r['id']);seen.add(key)
    if len(sample)>24:sample=[sample[i*(len(sample)-1)//23] for i in range(24)]
    result['representative_product_ids']=sample
    with Session() as s:
        latest=s.scalar(select(WorkbenchMeta).where(WorkbenchMeta.dataset_id==did,WorkbenchMeta.kind=='visual_overview').order_by(WorkbenchMeta.updated_at.desc(),WorkbenchMeta.id.desc()))
        if latest:
            value=latest.data
            result['latest_visual_overview']={'overview_id':latest.id,'goal':value.get('goal'),'sample_count':value.get('sample_count'),'successful':value.get('successful'),'input_policy':value.get('input_policy'),'basis':value.get('basis'),'groups':[{'patterns':g.get('patterns',[]),'coverage_note':g.get('coverage_note'),'results':g.get('results',[])} for g in value.get('groups',[]) if g.get('status')=='ready'],'created_at':value.get('created_at')}
    return result

def visual_overview(did,goal,limit=24):
    """Compare representative main images in six-product groups."""
    from concurrent.futures import ThreadPoolExecutor
    import threading,time
    from . import batch
    from .model_settings import defaults
    VISION_MODEL=defaults()[1]
    if not isinstance(goal,str) or not 3<=len(goal)<=2000:raise ValueError('请说明本次希望观察的结构或图案问题')
    if type(limit) is not int or not 1<=limit<=24:raise ValueError('代表样本数为1–24')
    summary=profile(did);ids=summary['representative_product_ids'][:limit];data=draft.sheet(did);rows=[r for r in data['rows'] if r['id'] in ids];metrics={};lock=threading.Lock();start=time.perf_counter()
    groups=[rows[i:i+6] for i in range(0,len(rows),6)]
    def observe(group):
        try:
            compared=batch.calibration_group(group,VISION_MODEL,goal,False,metrics,lock)
            by_id={item['product_id']:item for item in compared['products']}
            return [{'status':'ready','product_ids':[row['id'] for row in group],'patterns':compared['patterns'],'coverage_note':compared['coverage_note'],'cached':compared['cached'],'results':[{'product_id':row['id'],'row':row['row'],'anchor':row['anchor'],**by_id[row['id']]} for row in group]}]
        except Exception as e:
            if len(group)>1:
                middle=len(group)//2
                return observe(group[:middle])+observe(group[middle:])
            return [{'status':'failed','product_ids':[group[0]['id']],'error':str(e)[:500]}]
    with ThreadPoolExecutor(max_workers=3) as pool:group_results=[item for result in pool.map(observe,groups) for item in result]
    results=[item for group in group_results if group['status']=='ready' for item in group['results']]
    metrics['wall_seconds']=time.perf_counter()-start
    value={'goal':goal,'total':summary['total'],'sample_count':len(rows),'successful':len(results),'input_policy':'single_main_image_v1','group_size':6,'basis':'按去重主图稳定取代表样本，每组最多6件比较；结论只覆盖这些样本，不代表全量已看图','groups':group_results,'results':results,'metrics':metrics,'created_at':now()}
    with draft.WRITE_LOCK,Session.begin() as s:
        ident=uid();value['overview_id']=ident;record=WorkbenchMeta(id=ident,dataset_id=did,kind='visual_overview',data=value);s.add(record);audit(s,did,'visual_overview',{'overview_id':ident,'goal':goal,'sample_count':len(rows),'successful':value['successful'],'input_policy':value['input_policy']})
    return value
