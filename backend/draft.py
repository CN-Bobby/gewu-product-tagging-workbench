"""One continuously editable worksheet. All writes share the same transaction boundary."""
import copy
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

def migrate(did):
    """Idempotent import; legacy rows remain intact, no formal versions invented."""
    with WRITE_LOCK, Session.begin() as s:
        require(s,Dataset,did)
        if s.scalar(select(Audit.id).where(Audit.dataset_id==did,Audit.action=='draft_migration_v1')):return
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

def _sheet(s,did):
    d=require(s,Dataset,did)
    cols=[obj(x) for x in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did).order_by(DraftColumn.created_at,DraftColumn.id))]
    ps=s.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)).all()
    rows=[]
    for p in ps:
        assets=[obj(a) for a in s.scalars(select(Asset).where(Asset.product_id==p.id))]
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
        data.update(proposals=[obj(x) for x in s.scalars(select(Proposal).where(Proposal.dataset_id==did).order_by(Proposal.created_at.desc()))],jobs=[obj(x) for x in s.scalars(select(BatchJob).where(BatchJob.dataset_id==did).order_by(BatchJob.created_at.desc()))],versions=[{'id':x.id,'version':x.version,'created_at':x.created_at} for x in s.scalars(select(Snapshot).where(Snapshot.dataset_id==did).order_by(Snapshot.version))],messages=[obj(x) for x in s.scalars(select(Message).where(Message.dataset_id==did).order_by(Message.created_at))],cursor=s.scalar(select(func.max(DraftEvent.id)).where(DraftEvent.dataset_id==did)) or 0)
        from .task_control import brief
        data['jobs']=[brief(j)|{'archived':bool((s.get(WorkbenchMeta,'job:'+j['id']).data if s.get(WorkbenchMeta,'job:'+j['id']) else {}).get('archived'))} for j in data['jobs']]
    return data

def validate_definition(d,existing):
    standard=Definition.model_validate({k:v for k,v in d.items() if k in Definition.model_fields}).model_dump()
    standard.update({k:copy.deepcopy(d[k]) for k in ('method','depends_on','mapping','bins','fields','cardinality','when','parser') if k in d})
    if standard.get('cardinality','auto') not in ('auto','single','multiple'):raise ValueError('cardinality需为auto/single/multiple')
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

def propose(did,title,changes):
    migrate(did)
    if not isinstance(changes,list) or not changes or len(changes)>50:raise ValueError('提案需要1–50项变更')
    with WRITE_LOCK, Session.begin() as s:
        cols={c.id:c for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did))}
        pids=set(s.scalars(select(Product.id).where(Product.dataset_id==did)))
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
        p=Proposal(dataset_id=did,title=title,changes=clean)
        s.add(p);s.flush();emit(s,did,'proposal',obj(p));return obj(p)

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
            title='更新派生列：'+d['name']
            candidates=s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.status=='pending')).all()
            proposal=next((p for p in candidates if len(p.changes)==1 and p.changes[0].get('kind')=='execute' and p.changes[0].get('column_ids')==[col.id] and p.changes[0].get('automatic_dependency_update')),None)
            change={'kind':'execute','column_ids':[col.id],'product_ids':[pid],'revisions':{col.id:col.revision},'overwrite':True,'refresh_images':False,'automatic_dependency_update':True}
            if proposal:
                old=proposal.changes[0];change['product_ids']=list(dict.fromkeys(old['product_ids']+[pid]));proposal.changes=[change]
            else:proposal=Proposal(dataset_id=did,title=title,changes=[change]);s.add(proposal)
            s.flush();emit(s,did,'proposal',obj(proposal))
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
    col=require(s,DraftColumn,c.column_id)
    if c.revision!=col.revision:c.evidence={k:v for k,v in c.evidence.items() if k!='input_fingerprint'}
    if col.definition.get('attribute') and value is not None and type(value) is not bool:raise ValueError('属性单元格只能为 true/false/null')
    c.edit+=1;c.revision=require(s,DraftColumn,c.column_id).revision;c.manual={'value':value,'at':now()};c.value=value;c.status='ready' if value is not None else 'unknown';c.conflict=None
    if isinstance(value,list) and not col.definition.get('attribute'):
        if not value or any(not isinstance(v,str) or not v.strip() for v in value):raise ValueError('多个命中需为非空属性名称列表')
        c.value=list(dict.fromkeys(value))
        if len(c.value)>1 and not col.definition.get('expanded'):
            c.status='review';attributes.propose_expansion(s,did,col,col.definition.get('options',[])+c.value)
    cell_change(s,did,c,'manual_correction');recompute(s,did,c.product_id,c.column_id)

def confirm(did,ident,scope=None,sample=False,text_model=None,vision_model=None):
    from .model_settings import defaults
    DEFAULT_MODEL,VISION_MODEL=defaults()
    jobs=[]
    with WRITE_LOCK, Session.begin() as s:
        p=require(s,Proposal,ident)
        if p.dataset_id!=did or p.status!='pending':raise ValueError('提案已处理或不属于当前工作表')
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
                d={**copy.deepcopy(ch['definition']),'skill':tagging_rules.freeze()}
                if kind=='add':
                    if s.scalar(select(DraftColumn.id).where(DraftColumn.dataset_id==did,DraftColumn.key==d['key'])):raise ValueError('标签 key 已存在，请提出修改定义')
                    col=DraftColumn(id=ch.get('planned_id') or uid(),dataset_id=did,key=d['key'],definition=d,origin={'proposal_id':p.id,**({'template':ch['template_source']} if ch.get('template_source') else {})});s.add(col);s.flush()
                else:
                    col=require(s,DraftColumn,ch['column_id'])
                    if col.revision!=ch['expected_revision']:raise ValueError('提案基于旧定义，请重新提案')
                    audit(s,did,'column_revision',obj(col));col.revision+=1;col.definition=d
                for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):
                    c=get_cell(s,pid,col.id)
                    if not c.manual:
                        c.status='stale' if c.value is not None else 'pending'
                        if d.get('method') in ('mapping','bins','attribute'):calculate(s,did,pid,col)
                        else:cell_change(s,did,c,'column_changed')
                    recompute(s,did,pid,col.id)
                emit(s,did,'column',obj(col))
                if d.get('expanded'):
                    for child in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)):
                        if child.definition.get('parent_id')!=col.id:continue
                        a=child.definition['attribute'];child.revision+=1
                        child.definition={**child.definition,'definition':d['definition']+'；判断具体属性：'+a,'name':d['name']+'－'+a,'judgment':copy.deepcopy(d['judgment']),'source':d['source'],'fields':d.get('fields',d['judgment']['fields']),'method':'attribute' if d.get('method') in ('mapping','bins') else 'model','skill':copy.deepcopy(d['skill'])}
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
                    frozen['definition'].setdefault('skill',tagging_rules.freeze())
                    frozen['definition'].setdefault('judgment',tagging_rules.judgment(frozen['definition']))
                    columns.append(frozen)
                    for pid in ids:
                        c=get_cell(s,pid,cid);token=pid+':'+cid
                        if c.manual and not ch.get('overwrite'):continue
                        if c.status in ('ready','unknown','review') and c.revision==col.revision and not ch.get('overwrite'):continue
                        deps={dcid:{'value':get_cell(s,pid,dcid).value,'edit':get_cell(s,pid,dcid).edit,'revision':get_cell(s,pid,dcid).revision,'status':get_cell(s,pid,dcid).status} for dcid in col.definition.get('depends_on',[])}
                        expected[token]={'edit':c.edit,'revision':col.revision,'dependencies':deps}
                        from .task_control import claim
                        claim(s,did,token,jid)
                        c.status='pending';cell_change(s,did,c,'queued')
                job=BatchJob(id=jid,dataset_id=did,proposal_id=p.id,spec={'product_ids':ids,'columns':columns,'expected':expected,'text_model':text_model or DEFAULT_MODEL,'vision_model':vision_model or VISION_MODEL,'refresh_images':bool(ch.get('refresh_images')),'reuse_results':bool(ch.get('reuse_results')),'force_recompute':bool(ch.get('force_recompute')),'sample':sample},metrics={})
                s.add(job);s.flush();jobs.append(obj(job));emit(s,did,'job',obj(job))
        p.status='confirmed';p.confirmed_at=now();emit(s,did,'proposal',obj(p));audit(s,did,'human_confirmed_proposal',{'id':p.id,'scope':scope,'sample':sample})
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
