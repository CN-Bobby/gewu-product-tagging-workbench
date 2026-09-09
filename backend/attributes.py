"""Stable category/attribute columns; all expansion goes through human proposals."""
import copy
from sqlalchemy import select
from .db import *

def validate_result(value,definition):
    normalized=value.get('normalized_value');attrs=value.get('attributes',[])
    if not isinstance(attrs,list):raise ValueError('逐属性证据必须是数组')
    shape=definition.get('result_shape') or ('boolean' if definition.get('attribute') else 'multi_enum' if definition.get('cardinality')=='multiple' else 'scalar_enum' if definition.get('options') else 'scalar_open')
    if shape in ('scalar_open','scalar_enum'):
        if normalized is not None and not isinstance(normalized,(str,int,float)):raise ValueError('单值标签只能返回文本、数字或 null')
        if shape=='scalar_enum' and normalized is not None and normalized not in definition.get('options',[]):raise ValueError('结果不属于已确认候选值')
        if attrs:
            value['format_warnings']=list(dict.fromkeys(value.get('format_warnings',[])+['标量结果附带attributes，已忽略']))
            value['ignored_attributes']=attrs
            value.pop('attributes',None)
        return value
    seen=set()
    for a in attrs:
        if not isinstance(a,dict) or not isinstance(a.get('attribute'),str) or not a['attribute'].strip() or a['attribute'] in seen:raise ValueError('属性名称无效或重复')
        seen.add(a['attribute'])
        if a.get('value') is not None and type(a['value']) is not bool:raise ValueError('属性值只能是 true/false/null')
        if a.get('value') is not None and (not isinstance(a.get('evidence'),str) or not a['evidence'].strip()):raise ValueError('命中或否定必须有逐属性证据')
    if shape=='boolean':
        if normalized is not None and type(normalized) is not bool:raise ValueError('属性列需要布尔值或 null')
        if normalized is not None and not value.get('evidence','').strip():raise ValueError('属性判断缺少证据')
        own=next((a for a in attrs if a['attribute']==definition['attribute']),None)
        if own and own.get('value') is not normalized:raise ValueError('属性标准值与逐属性证据矛盾')
    elif shape=='multi_enum':
        positives={a['attribute'] for a in attrs if a.get('value') is True}
        if normalized is None:
            if positives:raise ValueError('未知结果不能包含明确正命中')
            return value
        if not isinstance(normalized,list) or not normalized or any(not isinstance(v,str) for v in normalized) or len(set(normalized))!=len(normalized):raise ValueError('多属性命中列表无效')
        if set(normalized)!=positives:raise ValueError('多属性命中缺少完整逐属性证据或包含矛盾')
        if definition.get('options') and any(v not in definition['options'] for v in normalized):raise ValueError('多属性结果不属于已确认候选值')
    return value

def propose_expansion(s,did,col,names):
    from . import draft
    parent=require(s,DraftColumn,col.definition.get('parent_id',col.id))
    existing=[c.definition['attribute'] for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)) if c.definition.get('parent_id')==parent.id]
    names=list(dict.fromkeys(n for n in names if isinstance(n,str) and n.strip() and n not in existing))
    if not names:return
    candidates=s.scalars(select(Proposal).where(Proposal.dataset_id==did,Proposal.status=='pending')).all()
    p=next((p for p in candidates if len(p.changes)==1 and p.changes[0].get('kind')=='expand' and p.changes[0].get('column_id')==parent.id),None)
    ch={'kind':'expand','column_id':parent.id,'attributes':names,'expected_revision':parent.revision}
    if p:
        ch['attributes']=list(dict.fromkeys(p.changes[0]['attributes']+names));p.changes=[ch]
    else:
        p=Proposal(dataset_id=did,title=('追加属性：' if parent.definition.get('expanded') else '整类拆列：')+parent.definition['name'],changes=[ch]);s.add(p)
    s.flush();draft.emit(s,did,'proposal',obj(p))

def expand(s,did,ch,proposal_id):
    from . import draft
    parent=require(s,DraftColumn,ch['column_id'])
    if parent.dataset_id!=did or parent.revision!=ch['expected_revision']:raise ValueError('类别已修改，请重新提出拆列')
    current=s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)).all()
    existing={c.definition.get('attribute'):c for c in current if c.definition.get('parent_id')==parent.id}
    names=list(dict.fromkeys(parent.definition.get('options',[])+ch['attributes']))
    audit(s,did,'category_expanded',{'parent':obj(parent),'attributes':names,'proposal_id':proposal_id})
    for name in names:
        if name in existing:continue
        key='attribute_'+uid()
        d={**copy.deepcopy(parent.definition),'key':key,'name':parent.definition['name']+'－'+name,'options':[],
           'definition':parent.definition['definition']+'；本列判断具体属性：'+name+'。只返回 true/false/null；未提及不等于否定。',
           'attribute':name,'parent_id':parent.id,'expanded':False,'result_shape':'boolean','method':'attribute' if parent.definition.get('method') in ('mapping','bins') else 'model','depends_on':[parent.id]}
        col=DraftColumn(dataset_id=did,key=key,definition=d,origin={'proposal_id':proposal_id,'category_id':parent.id})
        s.add(col);s.flush();draft.emit(s,did,'column',obj(col))
        for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):
            source=draft.get_cell(s,pid,parent.id);target=draft.get_cell(s,pid,col.id)
            trusted=source.status in ('ready','unknown','review')
            value,evidence,manual=expansion_evidence(s,pid,parent,name,existing.values())
            target.value=value;target.raw_value=source.raw_value;target.status='ready' if value is not None else 'unknown' if trusted else source.status
            if evidence:target.status='ready' if value is not None else 'review'
            target.evidence={'source_column':parent.id,'source_cell':obj(source),'attribute':name,'attribute_evidence':evidence,'reason':'仅迁移明确证据，其他留空'}
            if manual:target.manual={**manual,'value':value,'migrated_from':parent.id};target.edit=1
            draft.cell_change(s,did,target,'attribute_migrated')
    parent.revision+=1;parent.definition={**parent.definition,'expanded':True,'result_shape':'multi_enum'}
    draft.emit(s,did,'column',obj(parent))
    # Pending parent candidates are retained as a group observation after conversion.
    for source in s.scalars(select(Cell).where(Cell.column_id==parent.id)):
        if source.status=='review' and isinstance(source.value,list):source.status='ready'
        source.revision=parent.revision;draft.cell_change(s,did,source,'category_display_changed')

def expansion_evidence(s,pid,parent,name,children):
    from . import draft
    candidates=[]
    for col in [parent,*children]:
        cell=draft.get_cell(s,pid,col.id)
        if cell.status not in ('ready','unknown','review') or cell.revision!=col.revision:continue
        matches=col.id==parent.id and (cell.value==name or isinstance(cell.value,list) and name in cell.value)
        if cell.manual:
            if matches:return True,[{'column_id':col.id,'manual':cell.manual}],cell.manual
            continue
        attr=next((a for a in (cell.evidence or {}).get('attributes',[]) if a.get('attribute')==name and a.get('value') is not None),None)
        if attr or matches:
            candidates.append({'column_id':col.id,'cell_id':cell.id,'edit':cell.edit,'revision':cell.revision,'value':attr['value'] if attr else True,'evidence':attr or cell.evidence})
    values={c['value'] for c in candidates}
    return (next(iter(values)) if len(values)==1 else None),candidates,None

def on_result(s,job,col,cell,result):
    names=[a['attribute'] for a in result.get('attributes',[]) if a.get('value') is True]
    if col.definition.get('parent_id'):
        if names:propose_expansion(s,job.dataset_id,col,names)
    elif isinstance(cell.value,list):
        if len(cell.value)>1:
            cell.status='review'
            if col.definition.get('cardinality')=='single':cell.evidence={**cell.evidence,'review_reason':'互斥单选出现多个候选，需修正规则或结果；未自动拆列'}
            else:propose_expansion(s,job.dataset_id,col,col.definition.get('options',[])+names)
        elif len(cell.value)==1:cell.value=cell.value[0]

def collapse(s,did,parent):
    """Reversible display change; original children and their evidence remain in snapshots and draft."""
    from . import draft
    children=[c for c in s.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)) if c.definition.get('parent_id')==parent.id]
    for child in children:
        child.definition={**child.definition,'archived':True};child.revision+=1;draft.emit(s,did,'column',obj(child))
    parent.definition={**parent.definition,'expanded':False,'cardinality':'single','archived':False,'result_shape':'scalar_enum' if parent.definition.get('options') else 'scalar_open'};parent.revision+=1
    for pid in s.scalars(select(Product.id).where(Product.dataset_id==did)):
        cell=draft.get_cell(s,pid,parent.id)
        if cell.manual:continue
        positives=[(c,draft.get_cell(s,pid,c.id)) for c in children if draft.get_cell(s,pid,c.id).status=='ready' and draft.get_cell(s,pid,c.id).value is True]
        if positives:
            values=[c.definition['attribute'] for c,v in positives];cell.value=values[0] if len(values)==1 else values
            cell.evidence={**cell.evidence,'collapse_sources':[obj(v) for c,v in positives],'reason':'恢复单列；多个明确命中保留待确认，不自动取舍'}
            cell.status='ready' if len(values)==1 else 'review'
        else:cell.status='review' if cell.value is not None else 'unknown'
        cell.revision=parent.revision;draft.cell_change(s,did,cell,'category_collapsed');draft.recompute(s,did,pid,parent.id)
    draft.emit(s,did,'column',obj(parent))
