"""One continuously editable worksheet. All writes share the same transaction boundary."""
import copy
import hashlib
import json
import threading
from sqlalchemy import select, func
from .db import *
from .service import Definition
from . import tagging_rules,attributes

WRITE_LOCK = threading.RLock()
TRANSLATIONS = dict(zip(
    ['top + briefs','top + boyshort','top + skirted boyshort','top + swim shorts','top + briefs + swim shorts','top + briefs + cover skirt','square neck','halter','cross back','racer back','basic straps','floral','tropical palm','disney ip','mermaid','galaxy','geometric','solid mix','blue','purple','green/mint','warm multicolor','black & white','pink','toddler','young child','kid to teen','tween/teen','polyester + elastane','nylon/polyamide + elastane','100% nylon','budget (<$10)','mid ($10-15.99)','premium (≥$16)','2-piece set (2件套)','3-piece set (3件套)'],
    ['上衣＋三角裤','上衣＋平角裤','上衣＋裙式平角裤','上衣＋泳裤','上衣＋三角裤＋泳裤','上衣＋三角裤＋罩裙','方领','挂脖','交叉背带','工字背','基础肩带','花卉','热带棕榈','迪士尼角色','美人鱼','星空','几何','纯色拼接','蓝色系','紫色系','绿色／薄荷色系','暖色多彩','黑白','粉色系','幼儿','低龄儿童','儿童至青少年','大童／青少年','聚酯纤维＋弹性纤维','锦纶／聚酰胺＋弹性纤维','纯锦纶','低价（低于10美元）','中价（10–15.99美元）','高价（16美元及以上）','2件套','3件套']))

def emit(s, did, kind, payload):
    if kind=='job':
        from .task_control import brief
        payload=brief(payload)
    s.add(DraftEvent(dataset_id=did, kind=kind, payload=payload))

def translated(value):
    if isinstance(value,str):return TRANSLATIONS.get(value,value)
    return value

def get_cell(s, pid, cid):
    c=s.scalar(select(Cell).where(Cell.product_id==pid,Cell.column_id==cid))
    if not c:
        col=require(s,DraftColumn,cid)
        c=Cell(product_id=pid,column_id=cid,revision=col.revision,evidence={})
        s.add(c);s.flush()
    return c

def cell_change(s, did, c, action):
    s.flush()
    emit(s,did,'cell',obj(c))
    audit(s,did,action,obj(c))

def result_shape(definition):
    return definition.get('result_shape') or ('boolean' if definition.get('attribute') else 'multi_enum' if definition.get('cardinality')=='multiple' else 'scalar_enum' if definition.get('options') else 'scalar_open')

def migrate_definition_shapes(s,did):
    if s.scalar(select(Audit.id).where(Audit.dataset_id==did,Audit.action=='definition_shapes_v2')):return
    changed=0
    for col in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)):
        if 'result_shape' not in col.definition:
            col.definition={**col.definition,'result_shape':result_shape(col.definition)};changed+=1
    audit(s,did,'definition_shapes_v2',{'columns':changed,'rerun':False})

def revision_impact(before,after,requested=None):
    critical=('type','source','method','result_shape','fields','cardinality','when','parser','mapping','bins','depends_on','parent_id','attribute','target_scope','dimension','product_category','product_forms','input_policy','visual_standard_id')
    incompatible=any(before.get(k)!=after.get(k) for k in critical)
    incompatible=incompatible or not set(before.get('options',[])).issubset(set(after.get('options',[])))
    calculated='invalidate_all' if incompatible else 'preserve_compatible'
    if requested=='invalidate_all':return requested
    if requested not in (None,'preserve_compatible','invalidate_all'):raise ValueError('impact_mode无效')
    if requested=='preserve_compatible' and incompatible:raise ValueError('该修订改变语义或取值边界，不能保留旧结果')
    return calculated

def migrate(did):
    """Idempotent import; legacy rows remain intact, no formal versions invented."""
    with WRITE_LOCK, Session.begin() as s:
        require(s,Dataset,did)
        if s.scalar(select(Audit.id).where(Audit.dataset_id==did,Audit.action=='draft_migration_v1')):
            migrate_definition_shapes(s,did);return
        columns={}
        for schema in s.scalars(select(Schema).where(Schema.dataset_id==did,Schema.status=='locked').order_by(Schema.version)):
            for d in schema.definitions:
                signature=json.dumps(d,sort_keys=True,ensure_ascii=False)
                key=d['key']; col=columns.get(signature)
                if not col:
                    if s.scalar(select(DraftColumn.id).where(DraftColumn.dataset_id==did,DraftColumn.key==key)):key+=f'_legacy_{schema.version}'
                    definition=copy.deepcopy(d);definition['options']=[translated(x) for x in d.get('options',[])]
                    if key!=d['key']:definition['name']+=f'（原规则{schema.version}）'
                    col=DraftColumn(dataset_id=did,key=key,definition=definition,origin={'schema_id':schema.id,'schema_version':schema.version})
                    s.add(col);s.flush();columns[signature]=col
                runs=s.scalars(select(Run).where(Run.schema_id==schema.id).order_by(Run.created_at)).all()
                for run in runs:
                    for tag in s.scalars(select(Tag).where(Tag.run_id==run.id)):
                        vals=tag.values
                        if isinstance(vals,dict):vals=list(vals.values())
                        v=next((v for v in vals if v.get('key')==d['key']),None)
                        if not v:continue
                        c=get_cell(s,tag.product_id,col.id)
                        value=v.get('normalized_value'); corrected=tag.corrected or {}
                        if isinstance(corrected,dict) and d['key'] in corrected:
                            cv=corrected[d['key']];value=cv.get('normalized_value') if isinstance(cv,dict) else cv
                            c.manual={'value':translated(value),'at':tag.created_at,'legacy':True};c.edit=1
                        c.value=translated(value);c.raw_value=v.get('raw_value');c.evidence={**v,'legacy_run_id':run.id,'legacy_tag_id':tag.id,'original_normalized_value':v.get('normalized_value')}
                        c.status='unknown' if value is None else 'review' if isinstance(value,str) and value==translated(value) and any('a'<=x.lower()<='z' for x in value) else 'ready'
        for tr in s.scalars(select(Transform).join(Run,Transform.run_id==Run.id).where(Run.dataset_id==did,Transform.status=='approved').order_by(Transform.created_at)):
            run=require(s,Run,tr.run_id)
            candidates=s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)).all()
            source=next((c for c in reversed(candidates) if c.key==tr.source_key or (c.origin.get('schema_id')==run.schema_id and c.definition.get('key')==tr.source_key)),None)
            if not source:continue
            key=tr.output_key
            if any(c.key==key for c in candidates):key+='_'+tr.id[:8]
            mapping={str(translated(k)):translated(v) for k,v in tr.mapping.items()}
            col=DraftColumn(dataset_id=did,key=key,definition={'key':key,'name':translated(tr.output_key),'type':tr.type,'source':'text','definition':tr.definition,'options':list(dict.fromkeys(mapping.values())),'method':'mapping','depends_on':[source.id],'mapping':mapping},origin={'legacy_transform_id':tr.id,'legacy_run_id':run.id})
            s.add(col);s.flush()
            for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):calculate(s,did,pid,col)
        audit(s,did,'draft_migration_v1',{'columns':len(columns),'reversible':'legacy tables unchanged'})
        migrate_definition_shapes(s,did)

def _sheet(s,did):
    d=require(s,Dataset,did)
    cols=[obj(x) for x in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did).order_by(DraftColumn.created_at,DraftColumn.id))]
    ps=s.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)).all()
    rows=[]
    for p in ps:
        assets=[obj(a) for a in s.scalars(select(Asset).where(Asset.product_id==p.id).order_by(Asset.created_at,Asset.id))]
        cells={c.column_id:obj(c) for c in s.scalars(select(Cell).where(Cell.product_id==p.id))}
        record=s.scalar(select(TextRecord).where(TextRecord.product_id==p.id))
        rows.append({**obj(p),'assets':assets,'cells':cells,'text_record':obj(record) if record else None})
    from . import field_catalog
    return {'dataset':obj(d),'columns':cols,'rows':rows,'field_catalog':field_catalog.catalog(s,did)}

def sheet(did,version=None):
    migrate(did)
    with Session() as s:
        if version:
            snap=require(s,Snapshot,version)
            if snap.dataset_id!=did:raise ValueError('版本不属于当前数据集')
            return snap.data
        return _sheet(s,did)

def state(did):
    data=sheet(did)
    with Session() as s:
        migration=s.scalar(select(Audit).where(Audit.dataset_id==did,Audit.action=='draft_migration_v1'))
        data['legacy_message_ids']=list(s.scalars(select(Message.id).where(Message.dataset_id==did,Message.created_at<migration.created_at))) if migration else []
        data.update(proposals=[obj(x) for x in s.scalars(select(Proposal).where(Proposal.dataset_id==did).order_by(Proposal.created_at.desc()))],jobs=[obj(x) for x in s.scalars(select(BatchJob).where(BatchJob.dataset_id==did).order_by(BatchJob.created_at.desc()))],versions=[{'id':x.id,'version':x.version,'created_at':x.created_at} for x in s.scalars(select(Snapshot).where(Snapshot.dataset_id==did).order_by(Snapshot.version))],messages=[obj(x) for x in s.scalars(select(Message).where(Message.dataset_id==did).order_by(Message.created_at))],questions=[obj(x) for x in s.scalars(select(ChatQuestion).where(ChatQuestion.dataset_id==did).order_by(ChatQuestion.created_at))],cursor=s.scalar(select(func.max(DraftEvent.id)).where(DraftEvent.dataset_id==did)) or 0)
        from .task_control import brief
        data['jobs']=[brief(j)|{'archived':bool((s.get(WorkbenchMeta,'job:'+j['id']).data if s.get(WorkbenchMeta,'job:'+j['id']) else {}).get('archived'))} for j in data['jobs']]
        for question in data['questions']:
            meta=s.get(WorkbenchMeta,'business-question:'+question['id'])
            if meta:question.update(business=True,multiple=meta.data.get('multiple',False))
        from .chat_threads import decorate
        decorate(s,did,data)
    return data

def validate_definition(d,existing):
    standard=Definition.model_validate({k:v for k,v in d.items() if k in Definition.model_fields}).model_dump()
    if standard['source'] in ('image','both'):
        from .capabilities import require_visual_tagging
        require_visual_tagging()
    standard.update({k:copy.deepcopy(d[k]) for k in ('method','depends_on','mapping','bins','fields','cardinality','when','parser','result_shape','target_scope','dimension','product_category','product_forms','input_policy','visual_standard_id','expansion_policy','source_header','adopted_source','adoption_active','fill_empty_only','separator') if k in d})
    if standard.get('cardinality','auto') not in ('auto','single','multiple'):raise ValueError('cardinality需为auto/single/multiple')
    expansion_policy=standard.setdefault('expansion_policy','end_of_task_suggestion')
    if expansion_policy not in ('never','end_of_task_suggestion','manual_only'):raise ValueError('expansion_policy需为never/end_of_task_suggestion/manual_only')
    if standard.get('input_policy','single_main_image_v1') not in ('single_main_image_v1','single_main_with_quadrant_crops_v1','screenshot_grid_crops_v1','main_plus_supplemental_v1'):raise ValueError('未知图片输入策略')
    if standard['source']=='text' and standard.get('input_policy') not in (None,'single_main_image_v1'):raise ValueError('纯文字标签不能选择图片输入策略')
    if 'target_scope' in standard and (not isinstance(standard['target_scope'],str) or not standard['target_scope'].strip()):raise ValueError('target_scope需为非空文本')
    for routing_key in ('dimension','product_category'):
        if routing_key in standard and not (isinstance(standard[routing_key],str) or isinstance(standard[routing_key],list) and all(isinstance(x,str) for x in standard[routing_key])):raise ValueError(routing_key+'需为文本或字符串数组')
    if 'visual_standard_id' in standard and (standard['source'] not in ('image','both') or not isinstance(standard['visual_standard_id'],str)):raise ValueError('visual_standard_id只用于图片或图文标签')
    if 'product_forms' in standard and (not isinstance(standard['product_forms'],list) or not all(isinstance(x,str) for x in standard['product_forms'])):raise ValueError('product_forms需为字符串数组')
    standard['judgment']=tagging_rules.judgment(d)
    if d.get('parent_id'):
        if d['parent_id'] not in existing or not isinstance(d.get('attribute'),str):raise ValueError('属性列需要有效的父类别')
        standard.update(parent_id=d['parent_id'],attribute=d['attribute'])
    if d.get('expanded'):standard['expanded']=True
    method=standard.setdefault('method','model')
    if method not in ('model','mapping','bins','attribute','text_rule'):raise ValueError('处理方式需为 model / mapping / bins / attribute / text_rule')
    if method=='text_rule':
        from .text_rules import validate
        standard['parser']=validate(standard.get('parser',{}))
    if method=='attribute':
        if d.get('parent_id') not in existing or not isinstance(d.get('attribute'),str) or not d['attribute'].strip():raise ValueError('属性映射需要有效类别与属性名称')
        standard.update(parent_id=d['parent_id'],attribute=d['attribute'])
    shape=standard.get('result_shape')
    if shape is None:
        shape='boolean' if standard.get('attribute') else 'multi_enum' if standard.get('cardinality')=='multiple' else 'scalar_enum' if standard.get('options') else 'scalar_open'
    if shape not in ('scalar_open','scalar_enum','multi_enum','boolean'):raise ValueError('result_shape需为scalar_open/scalar_enum/multi_enum/boolean')
    if shape=='boolean' and not standard.get('attribute'):raise ValueError('boolean结果只用于拆分后的属性列')
    if shape=='scalar_enum' and not standard.get('options'):raise ValueError('scalar_enum需要非空候选值')
    if shape=='multi_enum' and standard.get('cardinality')=='single':raise ValueError('single标签不能使用multi_enum')
    standard['result_shape']=shape
    deps=standard.get('depends_on',[])
    if standard.get('when'):
        condition=standard['when']
        if not isinstance(condition,dict) or condition.get('column_id') not in existing or not isinstance(condition.get('values'),list):raise ValueError('when需要有效column_id和values')
        deps=list(dict.fromkeys(deps+[condition['column_id']]));standard['depends_on']=deps
    if not isinstance(deps,list) or not all(isinstance(x,str) for x in deps):raise ValueError('depends_on 需为列ID数组')
    if 'fields' in standard and (not isinstance(standard['fields'],list) or not all(isinstance(x,str) for x in standard['fields'])):raise ValueError('fields 需为字段名称数组')
    if any(x not in existing for x in deps):raise ValueError('依赖列不存在，请先确认上游列')
    if method in ('mapping','bins','attribute') and len(deps)!=1:raise ValueError('程序加工需要一个上游列')
    if method=='attribute' and deps!=[standard['parent_id']]:raise ValueError('属性映射必须依赖对应类别')
    if method=='mapping' and not isinstance(standard.get('mapping'),dict):raise ValueError('缺少映射字典')
    if method=='bins':
        bins=standard.get('bins')
        if not isinstance(bins,list) or not bins:raise ValueError('缺少数值区间')
        last_max=None
        for index,b in enumerate(bins):
            if not isinstance(b,dict) or not isinstance(b.get('value'),str):raise ValueError('区间需要中文 value')
            low,high=b.get('min'),b.get('max')
            if any(x is not None and not isinstance(x,(float,int)) for x in (low,high)):raise ValueError('区间上下界需为数值或 null')
            if low is not None and high is not None and low>=high:raise ValueError('区间下界必须小于上界')
            if index and (last_max is None or low is None or low<last_max):raise ValueError('区间需按升序排列且不得重叠')
            last_max=high
    return standard

def _split_execute(changes):
    result=[]
    for change in changes:
        columns=change.get('column_ids',[]) if change.get('kind')=='execute' else []
        if len(columns)<=1:
            result.append(copy.deepcopy(change));continue
        targets=change.get('target_product_ids',{})
        summaries={x.get('column_id'):x for x in change.get('repair_summary',[]) if isinstance(x,dict)}
        for cid in columns:
            item=copy.deepcopy(change);ids=list(targets.get(cid,change.get('product_ids',[])))
            item['column_ids']=[cid];item['product_ids']=ids
            if targets:item['target_product_ids']={cid:ids};item['targets']=[{'column_id':cid,'product_ids':ids}]
            if item.get('revisions'):item['revisions']={cid:item['revisions'][cid]}
            if summaries.get(cid):item['repair_summary']=[summaries[cid]]
            elif 'repair_summary' in item:item['repair_summary']=[]
            result.append(item)
    return result

def _planned_keys(changes):
    return {ch.get('planned_id'):'column-key:'+ch.get('definition',{}).get('key','') for ch in changes if ch.get('kind')=='add' and ch.get('planned_id')}

def _change_entities(change,planned):
    kind=change.get('kind')
    if kind=='add':return {'column-key:'+change.get('definition',{}).get('key','')}
    if kind in ('revise','expand','collapse','archive_column','restore_column'):
        return {'column:'+str(change.get('column_id'))}
    if kind=='execute':
        return {planned.get(cid,'column:'+str(cid)) for cid in change.get('column_ids',[])}
    if kind=='correct':
        return {'cell:'+str(change.get('column_id'))+':'+str(pid) for pid in change.get('product_ids',[])}
    return {'change:'+str(kind)}

def _proposal_entities(changes):
    changes=_split_execute(changes);planned=_planned_keys(changes)
    return set().union(*(_change_entities(ch,planned) for ch in changes)) if changes else set()

def _merge_changes(previous,new):
    previous=_split_execute(previous);new=_split_execute(new)
    previous_planned=_planned_keys(previous);new_planned=_planned_keys(new)
    incoming=set().union(*(_change_entities(ch,new_planned) for ch in new)) if new else set()
    kept=[ch for ch in previous if _change_entities(ch,previous_planned).isdisjoint(incoming)]
    return _combine_execute(kept+new)

def _combine_execute(changes):
    """Keep exact per-column targets while presenting compatible executions as one change."""
    result=[];groups={}
    excluded={'column_ids','product_ids','target_product_ids','targets','revisions','repair_summary'}
    for change in changes:
        if change.get('kind')!='execute':result.append(change);continue
        key=json.dumps({k:_canonical(v) for k,v in change.items() if k not in excluded},ensure_ascii=False,sort_keys=True)
        group=groups.get(key)
        if group is None:
            group=copy.deepcopy(change);group['column_ids']=[];group['product_ids']=[];group['target_product_ids']={};group['revisions']={};group['repair_summary']=[]
            groups[key]=group;result.append(group)
        for cid in change.get('column_ids',[]):
            if cid not in group['column_ids']:group['column_ids'].append(cid)
            ids=change.get('target_product_ids',{}).get(cid,change.get('product_ids',[]))
            group['target_product_ids'][cid]=list(dict.fromkeys(ids))
            group['product_ids']=list(dict.fromkeys(group['product_ids']+ids))
            if cid in change.get('revisions',{}):group['revisions'][cid]=change['revisions'][cid]
        group['repair_summary']+=copy.deepcopy(change.get('repair_summary',[]))
    for group in groups.values():
        group['targets']=[{'column_id':cid,'product_ids':group['target_product_ids'][cid]} for cid in group['column_ids']]
        if not group['repair_summary']:group.pop('repair_summary',None)
    return result

def _canonical(value,planned=None,key=None):
    planned=planned or {}
    if isinstance(value,str):return planned.get(value,value)
    if isinstance(value,dict):
        ignored={'planned_id','impact','repair_summary','actual_scope_computed'}
        return {k:_canonical(v,planned,k) for k,v in sorted(value.items()) if k not in ignored}
    if isinstance(value,list):
        items=[_canonical(v,planned,key) for v in value]
        if key in ('product_ids','column_ids','attributes'):return sorted(items,key=lambda x:json.dumps(x,ensure_ascii=False,sort_keys=True))
        return items
    return value

def proposal_fingerprint(changes):
    changes=_combine_execute(_split_execute(changes));planned={k:v for k,v in _planned_keys(changes).items()}
    payload=_canonical(changes,planned)
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _active_turn(s,did):
    item=s.get(WorkbenchMeta,'active-chat:'+did)
    return item.data.get('turn_id') if item and item.kind=='active_chat_turn' else None

def _store_proposal(s,did,title,changes,origin_turn_id=None):
    changes=copy.deepcopy(changes);fingerprint=proposal_fingerprint(changes)
    pending=list(s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.status=='pending').order_by(Proposal.created_at)))
    duplicate=next((p for p in reversed(pending) if (p.fingerprint or proposal_fingerprint(p.changes or []))==fingerprint),None)
    if duplicate:
        if not duplicate.fingerprint:duplicate.fingerprint=fingerprint
        result=obj(duplicate);result['merge_action']='reused';return result
    entities=_proposal_entities(changes)
    related=[]
    for proposal in pending:
        same_turn=bool(origin_turn_id and proposal.origin_turn_id==origin_turn_id)
        overlaps=not _proposal_entities(proposal.changes or []).isdisjoint(entities)
        if same_turn or overlaps:related.append(proposal)
    merged=[]
    for proposal in related:merged=_merge_changes(merged,proposal.changes or [])
    merged=_merge_changes(merged,changes) if related else changes
    ident=uid();group_id=(related[-1].group_id or related[-1].id) if related else ident
    revision=max([p.group_revision or 1 for p in related],default=0)+1
    record=Proposal(id=ident,dataset_id=did,group_id=group_id,group_revision=revision,
                    fingerprint=proposal_fingerprint(merged),origin_turn_id=origin_turn_id,
                    title=title,changes=merged)
    s.add(record)
    for proposal in related:
        proposal.status='superseded';proposal.superseded_by=ident;emit(s,did,'proposal',obj(proposal));emit(s,did,'proposal_group',obj(proposal))
    s.flush();emit(s,did,'proposal',obj(record));emit(s,did,'proposal_group',obj(record))
    result=obj(record);result['merge_action']='merged' if related else 'created';return result

def propose(did,title,changes,origin_turn_id=None,use_active_turn=False):
    migrate(did)
    if not isinstance(changes,list) or not changes or len(changes)>50:raise ValueError('提案需要1–50项变更')
    with WRITE_LOCK, Session.begin() as s:
        cols={c.id:c for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did))}
        product_order=list(s.scalars(select(Product.id).where(Product.dataset_id==did).order_by(Product.row)))
        pids=set(product_order)
        clean=copy.deepcopy(changes)
        from types import SimpleNamespace
        refs={}
        for ch in clean:
            if ch.get('kind')=='add':
                ch['planned_id']=uid();refs['$'+ch['definition']['key']]=ch['planned_id']
        def resolve_refs(value):
            if isinstance(value,str):return refs.get(value,value)
            if isinstance(value,list):return [resolve_refs(v) for v in value]
            if isinstance(value,dict):return {k:resolve_refs(v) for k,v in value.items()}
            return value
        clean=resolve_refs(clean)
        for change in clean:
            if change.get('kind')=='execute' and change.get('repair_scope_id'):
                saved=s.get(WorkbenchMeta,change['repair_scope_id'])
                if not saved or saved.dataset_id!=did or saved.kind!='repair_scope':raise ValueError('修复范围不存在或不属于当前数据集')
                change['targets']=saved.data.get('targets',[])
            if change.get('kind')=='execute' and change.get('targets') is not None:
                targets=change['targets']
                if not isinstance(targets,list) or not targets:raise ValueError('targets需要至少一个列范围')
                target_ids={};all_ids=[]
                for target in targets:
                    if not isinstance(target,dict) or not isinstance(target.get('column_id'),str):raise ValueError('target需要column_id')
                    cid=target['column_id'];ids=target.get('product_ids',[])
                    if not isinstance(ids,list) or not ids:raise ValueError('每个target都需要非空product_ids')
                    if cid in target_ids:raise ValueError('targets中的column_id不可重复')
                    target_ids[cid]=list(dict.fromkeys(ids));all_ids.extend(ids)
                change['column_ids']=list(target_ids);change['target_product_ids']=target_ids;change['product_ids']=list(dict.fromkeys(all_ids))
        for change in clean:
            kind=change.get('kind')
            if kind in ('add','revise'):
                change['definition']=validate_definition(change['definition'],cols)
                if kind=='revise':
                    col=cols.get(change.get('column_id'))
                    if not col:raise ValueError('列不存在')
                    change['definition']['key']=col.key
                    for key in ('attribute','parent_id','expanded','archived'):
                        if key in col.definition:change['definition'][key]=col.definition[key]
                    change['expected_revision']=col.revision
                    change['impact_mode']=revision_impact(col.definition,change['definition'],change.get('impact_mode'))
                    statuses=list(s.scalars(select(Cell.status).where(Cell.column_id==col.id)))
                    change['impact']={'total':len(pids),'preserved':sum(x in ('ready','unknown','review') for x in statuses) if change['impact_mode']=='preserve_compatible' else 0,'invalidated':sum(x in ('ready','unknown','review','failed','stale') for x in statuses) if change['impact_mode']=='invalidate_all' else 0}
                    deps=change['definition'].get('depends_on',[])
                    def reaches(cid,seen):
                        if cid==col.id:return True
                        if cid in seen:return False
                        return any(reaches(x,seen|{cid}) for x in cols[cid].definition.get('depends_on',[]))
                    if any(reaches(x,set()) for x in deps):raise ValueError('派生列不能循环依赖')
                    cols[col.id]=SimpleNamespace(id=col.id,key=col.key,revision=col.revision+1,definition=change['definition'])
                else:
                    cid=change['planned_id'];cols[cid]=SimpleNamespace(id=cid,key=change['definition']['key'],revision=1,definition=change['definition'])
            elif kind=='expand':
                col=cols.get(change.get('column_id'))
                if not col or col.definition.get('parent_id'):raise ValueError('请选择种类列')
                names=change.get('attributes')
                if not isinstance(names,list) or not names or len(names)>100 or any(not isinstance(n,str) or not n.strip() or len(n)>100 for n in names):raise ValueError('属性名称列表无效')
                change['attributes']=list(dict.fromkeys(names));change['expected_revision']=col.revision
            elif kind in ('collapse','archive_column','restore_column'):
                col=cols.get(change.get('column_id'))
                if not col:raise ValueError('列不存在')
                if kind=='collapse' and (not col.definition.get('expanded') or col.definition.get('parent_id')):raise ValueError('请选择已拆列类别')
                change['expected_revision']=col.revision
                change['definition']=copy.deepcopy(col.definition)
            elif kind=='execute':
                if not change.get('column_ids') or any(x not in cols for x in change['column_ids']):raise ValueError('请选择当前草稿中的标签列')
                if len(change['column_ids'])!=1 and not change.get('target_product_ids'):raise ValueError('多列执行必须使用targets为每列明确商品范围')
                if any(cols[x].definition.get('source') in ('image','both') for x in change['column_ids']):
                    from .capabilities import require_visual_tagging
                    require_visual_tagging()
                if any(cols[x].definition.get('expanded') for x in change['column_ids']):raise ValueError('种类已拆列，请选择具体属性列执行')
                if any(cols[x].definition.get('archived') for x in change['column_ids']):raise ValueError('请先恢复已归档列')
                change['revisions']={x:cols[x].revision for x in change['column_ids']}
            elif kind=='correct':
                if change.get('column_id') not in cols:raise ValueError('列不存在')
            else:raise ValueError('未知变更类型')
            if kind in ('execute','correct'):
                if change.get('scope_id'):
                    from .worksheet_query import resolve_scope
                    change['product_ids']=resolve_scope(s,did,change['scope_id'])
                ids=change.get('product_ids',list(pids))
                if not ids or any(x not in pids for x in ids):raise ValueError('商品范围无效')
                change['product_ids']=list(dict.fromkeys(ids))
                if kind=='correct':
                    change['expected_edits']={pid:get_cell(s,pid,change['column_id']).edit for pid in ids}
        invalidated={ch['column_id']:ch for ch in clean if ch.get('kind')=='revise' and ch.get('impact_mode')=='invalidate_all'}
        for change in clean:
            if change.get('kind')!='execute':continue
            affected=[cid for cid in change['column_ids'] if cid in invalidated]
            if not affected:continue
            targets={cid:list(change.get('target_product_ids',{}).get(cid,change['product_ids'])) for cid in change['column_ids']}
            for cid in affected:targets[cid]=list(product_order)
            change['targets']=[{'column_id':cid,'product_ids':targets[cid]} for cid in change['column_ids']]
            change['target_product_ids']=targets
            change['product_ids']=list(dict.fromkeys(pid for cid in change['column_ids'] for pid in targets[cid]))
            change['actual_scope_computed']=True
            summaries={item['column_id']:item for item in change.get('repair_summary',[])}
            for cid in affected:
                col=cols[cid];cells=list(s.scalars(select(Cell).where(Cell.column_id==cid)))
                protected=sum(bool(cell.manual) for cell in cells);method=col.definition.get('method','model')
                summaries[cid]={'column_id':cid,'name':col.definition['name'],'failed':summaries.get(cid,{}).get('failed',0),
                                'rerun':len(product_order)-protected,'preserved_success':0,'protected':protected,
                                'estimated_requests':0 if method in ('text_rule','mapping','bins','attribute') else (len(product_order)-protected+4)//5,
                                'human_values_protected':True,'reason':'定义语义或执行契约改变，后端按整列计算实际影响范围'}
            change['repair_summary']=list(summaries.values())
        if use_active_turn and not origin_turn_id:origin_turn_id=_active_turn(s,did)
        return _store_proposal(s,did,title,clean,origin_turn_id)

def recompute(s,did,pid,upstream,visited=None):
    visited=visited or set()
    for col in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)):
        d=col.definition
        if upstream not in d.get('depends_on',[]) or col.id in visited:continue
        c=get_cell(s,pid,col.id)
        if c.manual:continue
        if d.get('method') in ('mapping','bins','attribute'):calculate(s,did,pid,col)
        else:
            c.status='stale';cell_change(s,did,c,'dependency_stale')
            title='更新受影响的派生标签'
            candidates=s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.status=='pending')).all()
            proposal=next((p for p in candidates if any(ch.get('automatic_dependency_update') and ch.get('automatic_source_column')==upstream for ch in (p.changes or []))),None)
            change={'kind':'execute','column_ids':[col.id],'product_ids':[pid],'revisions':{col.id:col.revision},'overwrite':True,'refresh_images':False,'automatic_dependency_update':True,'automatic_source_column':upstream}
            if proposal:
                changes=copy.deepcopy(proposal.changes or [])
                old=next((x for x in changes if x.get('column_ids')==[col.id]),None)
                if old:
                    change['product_ids']=list(dict.fromkeys(old['product_ids']+[pid]));changes[changes.index(old)]=change
                else:changes.append(change)
                proposal.changes=changes;proposal.fingerprint=proposal_fingerprint(changes)
            else:
                ident=uid();proposal=Proposal(id=ident,dataset_id=did,group_id=ident,group_revision=1,title=title,changes=[change],fingerprint=proposal_fingerprint([change]));s.add(proposal)
            s.flush();emit(s,did,'proposal',obj(proposal));emit(s,did,'proposal_group',obj(proposal))
        recompute(s,did,pid,col.id,visited|{col.id})

def derived_value(d,source_value,source_status):
    value=None
    if source_status in ('ready','unknown'):
        if d['method']=='attribute':value=True if source_value==d['attribute'] or isinstance(source_value,list) and d['attribute'] in source_value else None
        elif d['method']=='mapping':value=d['mapping'].get(str(source_value),d['mapping'].get(json.dumps(source_value,ensure_ascii=False)))
        else:
            try:
                number=float(source_value)
                value=next((b['value'] for b in d['bins'] if (b.get('min') is None or number>=b['min']) and (b.get('max') is None or number<b['max'])),None)
            except (ValueError,TypeError):pass
    return value,('ready' if value is not None else 'stale' if source_status not in ('ready','unknown') else 'unknown')

def calculate(s,did,pid,col):
    d=col.definition; source=get_cell(s,pid,d['depends_on'][0]);c=get_cell(s,pid,col.id)
    if c.manual:return c
    value,status=derived_value(d,source.value,source.status)
    c.value=value;c.revision=col.revision;c.status=status
    c.evidence={'method':d['method'],'source_column':source.column_id,'source_value':source.value,'rule':d};c.raw_value=source.value
    cell_change(s,did,c,'derived_recalculated');return c

def correct(did,pid,cid,value,expected_edit=None):
    with WRITE_LOCK, Session.begin() as s:
        col=require(s,DraftColumn,cid);p=require(s,Product,pid)
        if col.dataset_id!=did or p.dataset_id!=did:raise ValueError('单元格不在当前数据集')
        c=get_cell(s,pid,cid)
        if expected_edit is not None and c.edit!=expected_edit:raise ValueError('单元格已发生变更，请刷新后修改')
        _correct(s,did,c,value)
        return obj(c)

def _correct(s,did,c,value):
    from .task_control import settle_owned_token
    col=require(s,DraftColumn,c.column_id)
    if c.revision!=col.revision:c.evidence={k:v for k,v in c.evidence.items() if k!='input_fingerprint'}
    if col.definition.get('attribute') and value is not None and type(value) is not bool:raise ValueError('属性单元格只能为 true/false/null')
    c.edit+=1;c.revision=require(s,DraftColumn,c.column_id).revision;c.manual={'value':value,'at':now()};c.value=value;c.status='ready' if value is not None else 'unknown';c.conflict=None
    if isinstance(value,list) and not col.definition.get('attribute'):
        if not value or any(not isinstance(v,str) or not v.strip() for v in value):raise ValueError('多个命中需为非空属性名称列表')
        c.value=list(dict.fromkeys(value))
        if len(c.value)>1 and not col.definition.get('expanded'):
            c.status='review';attributes.record_definition_gap(s,did,col,col.definition.get('options',[])+c.value,product_id=c.product_id)
    settle_owned_token(s,did,c.product_id+':'+c.column_id,'人工修正')
    cell_change(s,did,c,'manual_correction');recompute(s,did,c.product_id,c.column_id)

def attach_visual_standard(s,did,definition,proposal_id):
    ident=definition.get('visual_standard_id')
    if not ident:return definition
    item=require(s,WorkbenchMeta,ident)
    if item.dataset_id!=did or item.kind!='visual_overview' or not isinstance(item.data.get('visual_standard'),dict):raise ValueError('视觉标准不存在或不属于当前数据集')
    standard=copy.deepcopy(item.data['visual_standard']);standard['confirmed']=True;standard['confirmed_at']=now();standard['confirmed_by_proposal']=proposal_id
    data=dict(item.data);data['visual_standard']=standard;item.data=data;item.updated_at=now()
    compact={k:standard.get(k) for k in ('scope','sample_ids','sample_count','total','coverage','candidate_values','boundaries','unresolved','stability','revision','confirmed')}
    compact['overview_id']=ident;compact['sha256']=hashlib.sha256(json.dumps(compact,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return {**definition,'visual_standard':compact}

def confirm(did,ident,scope=None,sample=False,text_model=None,vision_model=None,confirmation_key=None,text_credential_id=None,vision_credential_id=None):
    from .model_settings import defaults
    DEFAULT_MODEL,VISION_MODEL=defaults()
    jobs=[]
    with WRITE_LOCK, Session.begin() as s:
        p=require(s,Proposal,ident)
        if p.dataset_id!=did:raise ValueError('提案不属于当前工作表')
        if p.status=='confirmed' and confirmation_key and p.confirmation_key==confirmation_key:
            return [obj(j) for j in s.scalars(select(BatchJob).where(BatchJob.proposal_id==p.id).order_by(BatchJob.created_at))]
        if p.status!='pending':raise ValueError('提案已处理、已替换或不可确认')
        if confirmation_key is not None and (not isinstance(confirmation_key,str) or not confirmation_key.strip() or len(confirmation_key)>100):raise ValueError('确认请求标识无效')
        from .task_control import validate_repair_scope
        for ch in p.changes:
            if ch.get('kind')=='execute' and ch.get('repair_scope_id'):
                item=require(s,WorkbenchMeta,ch['repair_scope_id'])
                validate_repair_scope(s,did,item.data)
        from .capabilities import require_visual_tagging
        for ch in p.changes:
            if ch['kind'] in ('add','revise') and ch['definition'].get('source') in ('image','both'):
                require_visual_tagging()
            if ch['kind']=='execute':
                for cid in ch['column_ids']:
                    source=next((x['definition'].get('source') for x in p.changes if x.get('planned_id')==cid),None)
                    if source is None:source=require(s,DraftColumn,cid).definition.get('source')
                    if source in ('image','both'):require_visual_tagging()
        proposed_ids=list(dict.fromkeys(pid for ch in p.changes if ch['kind']=='execute' for pid in ch['product_ids']))
        execution_scope=None
        if scope is not None:
            if not scope or any(pid not in proposed_ids for pid in scope):raise ValueError('确认范围必须在提案范围内')
            execution_scope=list(dict.fromkeys(scope))
        if sample:
            execution_scope=(execution_scope if execution_scope is not None else proposed_ids)[:20]
        for ch in p.changes:
            kind=ch['kind']
            if kind in ('add','revise'):
                d=tagging_rules.prepare_definition(attach_visual_standard(s,did,copy.deepcopy(ch['definition']),p.id))
                if kind=='add':
                    if s.scalar(select(DraftColumn.id).where(DraftColumn.dataset_id==did,DraftColumn.key==d['key'])):raise ValueError('标签 key 已存在，请提出修改定义')
                    col=DraftColumn(id=ch.get('planned_id') or uid(),dataset_id=did,key=d['key'],definition=d,origin={'proposal_id':p.id,**({'template':ch['template_source']} if ch.get('template_source') else {})});s.add(col);s.flush()
                else:
                    col=require(s,DraftColumn,ch['column_id'])
                    if col.revision!=ch['expected_revision']:raise ValueError('提案基于旧定义，请重新提案')
                    audit(s,did,'column_revision',obj(col));col.revision+=1;col.definition=d
                for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):
                    c=get_cell(s,pid,col.id)
                    if kind=='revise' and ch.get('impact_mode')=='preserve_compatible':
                        if not c.manual:
                            c.revision=col.revision
                            if c.status in ('ready','unknown','review'):
                                c.evidence={**(c.evidence or {}),'accepted_through_revision':col.revision,'preserved_by_proposal':p.id}
                            cell_change(s,did,c,'column_revision_preserved')
                    elif not c.manual:
                        c.status='stale' if c.value is not None else 'pending'
                        if d.get('method') in ('mapping','bins','attribute'):calculate(s,did,pid,col)
                        else:cell_change(s,did,c,'column_changed')
                    if not (kind=='revise' and ch.get('impact_mode')=='preserve_compatible'):recompute(s,did,pid,col.id)
                emit(s,did,'column',obj(col))
                if d.get('expanded'):
                    for child in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)):
                        if child.definition.get('parent_id')!=col.id:continue
                        a=child.definition['attribute'];child.revision+=1
                        child_definition={**child.definition,'definition':d['definition']+'；判断具体属性：'+a,'name':d['name']+'－'+a,'judgment':copy.deepcopy(d['judgment']),'source':d['source'],'fields':d.get('fields',d['judgment']['fields']),'method':'attribute' if d.get('method') in ('mapping','bins') else 'model','skill':copy.deepcopy(d['skill'])}
                        for key in ('target_scope','dimension','product_category','product_forms','input_policy','visual_standard','visual_standard_id','expansion_policy'):
                            if key in d:child_definition[key]=copy.deepcopy(d[key])
                        child_definition['runtime_profile']=tagging_rules.compile_runtime_profile(child_definition,'execution',child_definition['skill']);child.definition=child_definition
                        emit(s,did,'column',obj(child))
                    for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):recompute(s,did,pid,col.id)
            elif kind=='expand':attributes.expand(s,did,ch,p.id)
            elif kind in ('collapse','archive_column','restore_column'):
                col=require(s,DraftColumn,ch['column_id'])
                if col.revision!=ch['expected_revision']:raise ValueError('列已改变，请重新提案')
                affected={col.id}|{c.id for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)) if c.definition.get('parent_id')==col.id}
                for job in s.scalars(select(BatchJob).where(BatchJob.dataset_id==did,BatchJob.status.in_(['running','queued','pausing','cancelling']))):
                    if any(c['id'] in affected for c in job.spec.get('columns',[])):raise ValueError('请先暂停相关列的任务，再调整展示结构')
                if kind=='collapse':attributes.collapse(s,did,col)
                else:
                    col.revision+=1;col.definition={**col.definition,'archived':kind=='archive_column'};emit(s,did,'column',obj(col))
                audit(s,did,kind,{'column_id':col.id,'proposal_id':p.id});emit(s,did,'reload',{'reason':'标签展示结构已改变'})
            elif kind=='correct':
                for pid in ch['product_ids']:
                    c=get_cell(s,pid,ch['column_id'])
                    if c.edit!=ch['expected_edits'][pid]:raise ValueError('提案后该单元格已被修改，请重新提案')
                    _correct(s,did,c,ch.get('value'))
            elif kind=='execute':
                ids=ch['product_ids']
                jid=uid()
                if execution_scope is not None:
                    ids=[pid for pid in ids if pid in execution_scope]
                    if not ids:continue
                columns=[];expected={}
                for cid in ch['column_ids']:
                    col=require(s,DraftColumn,cid)
                    if col.revision!=ch['revisions'][cid]:raise ValueError('规则已改变，请重新提出执行提案')
                    frozen=copy.deepcopy(obj(col))
                    if 'skill' not in frozen['definition']:
                        frozen['definition']=tagging_rules.prepare_definition(frozen['definition'])
                    elif 'runtime_profile' not in frozen['definition']:
                        frozen['definition']['runtime_profile']=tagging_rules.compile_runtime_profile(frozen['definition'],'execution',frozen['definition']['skill'])
                    frozen['definition'].setdefault('judgment',tagging_rules.judgment(frozen['definition']))
                    columns.append(frozen)
                    column_ids=ch.get('target_product_ids',{}).get(cid,ids)
                    if execution_scope is not None:column_ids=[pid for pid in column_ids if pid in execution_scope]
                    for pid in column_ids:
                        c=get_cell(s,pid,cid);token=pid+':'+cid
                        if c.manual and not ch.get('overwrite'):continue
                        if c.status in ('ready','unknown','review') and c.revision==col.revision and not ch.get('overwrite'):continue
                        deps={dcid:{'value':get_cell(s,pid,dcid).value,'edit':get_cell(s,pid,dcid).edit,'revision':get_cell(s,pid,dcid).revision,'status':get_cell(s,pid,dcid).status} for dcid in col.definition.get('depends_on',[])}
                        expected[token]={'edit':c.edit,'revision':col.revision,'dependencies':deps}
                        from .task_control import claim
                        claim(s,did,token,jid)
                        c.status='pending';cell_change(s,did,c,'queued')
                job=BatchJob(id=jid,dataset_id=did,proposal_id=p.id,spec={'product_ids':ids,'columns':columns,'expected':expected,'text_model':text_model or DEFAULT_MODEL,'vision_model':vision_model or VISION_MODEL,'text_credential_id':text_credential_id,'vision_credential_id':vision_credential_id,'refresh_images':bool(ch.get('refresh_images')),'reuse_results':bool(ch.get('reuse_results')),'force_recompute':bool(ch.get('force_recompute')),'sample':sample},metrics={})
                s.add(job);s.flush();jobs.append(obj(job));emit(s,did,'job',obj(job))
        p.status='confirmed';p.confirmed_at=now();p.confirmation_key=confirmation_key;emit(s,did,'proposal',obj(p));emit(s,did,'proposal_group',obj(p));audit(s,did,'human_confirmed_proposal',{'id':p.id,'scope':scope,'sample':sample,'confirmation_key':confirmation_key})
    return jobs

def write_result(s,job,pid,col,value):
    token=pid+':'+col['id']; expected=job.spec['expected'][token]
    from .task_control import owns,reject_candidate
    if not owns(s,job,token):
        reject_candidate(s,job.id,job.spec.get('text_model'),{'code':'superseded','token':token,'candidate':value});job.completed=list(set(job.completed+[token]));return
    c=get_cell(s,pid,col['id']);current=require(s,DraftColumn,col['id'])
    skill=col['definition'].get('skill',{})
    from .build_info import build_info
    evidence={**value,'job_id':job.id,'application_build':job.spec.get('application_build'),'execution_build':build_info(),'column_revision':col['revision'],'skill_version':skill.get('version'),'skill_sha256':skill.get('sha256'),'judgment':col['definition'].get('judgment')}
    deps_changed=any(any(getattr(get_cell(s,pid,cid),key)!=val for key,val in old.items()) for cid,old in expected.get('dependencies',{}).items())
    if c.edit!=expected['edit'] or current.revision!=expected['revision'] or deps_changed:
        c.conflict=evidence
        if not c.manual:c.status='stale'
        cell_change(s,job.dataset_id,c,'worker_conflict')
    else:
        c.value=value.get('normalized_value');c.raw_value=value.get('raw_value');c.evidence=evidence;c.revision=col['revision'];c.status='ready' if c.value is not None else 'unknown';c.conflict=None;c.manual=None
        if col['definition'].get('method') in ('mapping','bins','attribute'):c.status=value.get('result_status',c.status)
        attributes.on_result(s,job,current,c,value)
        from .result_reuse import index
        s.flush();index(s,c)
        cell_change(s,job.dataset_id,c,'worker_result');recompute(s,job.dataset_id,pid,col['id'])
    job.completed=list(set(job.completed+[token]))

def save_version(did):
    with WRITE_LOCK, Session.begin() as s:
        if s.scalar(select(BatchJob.id).where(BatchJob.dataset_id==did,BatchJob.status.in_(['running','queued','pausing','cancelling']))):raise ValueError('请先暂停运行中的任务，待请求保存完成后再保存版本')
        n=(s.scalar(select(func.max(Snapshot.version)).where(Snapshot.dataset_id==did)) or 0)+1
        snap=Snapshot(dataset_id=did,version=n,data=copy.deepcopy(_sheet(s,did)));s.add(snap);s.flush()
        item={'id':snap.id,'version':n,'created_at':snap.created_at};emit(s,did,'version',item);return item

def compare(did,left,right=None):
    a=sheet(did,left);b=sheet(did,right);changes=[]
    ac={x['id']:x for x in a['columns']};bc={x['id']:x for x in b['columns']}
    for cid in ac.keys()|bc.keys():
        if ac.get(cid)!=bc.get(cid):changes.append({'kind':'definition','column':(bc.get(cid) or ac[cid])['definition']['name'],'before':ac.get(cid),'after':bc.get(cid)})
    ar={r['id']:r for r in a['rows']}
    for row in b['rows']:
        for cid,c in row['cells'].items():
            old=ar.get(row['id'],{}).get('cells',{}).get(cid,{})
            if any(old.get(k)!=c.get(k) for k in ('value','status','manual','revision')):changes.append({'kind':'cell','asin':row['anchor'],'column':bc[cid]['definition']['name'],'before':old.get('value'),'after':c['value'],'before_status':old.get('status'),'after_status':c['status']})
    return changes
