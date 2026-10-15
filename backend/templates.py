"""Project-scoped immutable tag standards; copying always produces a confirmation proposal."""
import copy,hashlib
from sqlalchemy import select,func
from .db import *
from . import draft


def projects():
    from . import project_files
    with Session() as s:items=[obj(w) for w in s.scalars(select(Workspace).order_by(Workspace.created_at))]
    return [{**item, **project_files.public_project(item['id'])} for item in items]


def create_project(name):
    if not isinstance(name,str) or not 1<=len(name.strip())<=100:raise ValueError('项目名称需为1–100字')
    with Session.begin() as s:
        w=Workspace(name=name.strip());s.add(w);s.flush();result=obj(w)
    from . import project_files
    project_files.ensure_project(result['id'])
    return {**result, **project_files.public_project(result['id'])}


def list_templates(workspace_id):
    with Session() as s:
        require(s,Workspace,workspace_id)
        return [{**obj(t),'revisions':[obj(r) for r in s.scalars(select(TemplateRevision).where(TemplateRevision.template_id==t.id).order_by(TemplateRevision.revision.desc()))]}
                for t in s.scalars(select(TagTemplate).where(TagTemplate.workspace_id==workspace_id))]


def save(did,name,column_ids,template_id=None):
    data=draft.sheet(did)
    if not name or not isinstance(column_ids,list) or not column_ids:raise ValueError('请输入模板名称并选择标签列')
    columns={c['id']:c for c in data['columns']}
    ordered=[];seen=set()
    def include(cid):
        if cid in seen:return
        if cid not in columns:raise ValueError('模板列不属于当前工作表')
        seen.add(cid);c=columns[cid]
        if c['definition'].get('archived'):raise ValueError('模板不能包含归档列')
        for dep in c['definition'].get('depends_on',[]):include(dep)
        if c['definition'].get('parent_id'):include(c['definition']['parent_id'])
        ordered.append(copy.deepcopy(c))
    for cid in column_ids:include(cid)
    with draft.WRITE_LOCK,Session.begin() as s:
        ds=require(s,Dataset,did)
        if template_id:
            template=require(s,TagTemplate,template_id)
            if template.workspace_id!=ds.workspace_id:raise ValueError('模板属于其他项目')
        else:
            template=TagTemplate(workspace_id=ds.workspace_id,name=name.strip());s.add(template);s.flush()
        revision=(s.scalar(select(func.max(TemplateRevision.revision)).where(TemplateRevision.template_id==template.id)) or 0)+1
        record=TemplateRevision(template_id=template.id,revision=revision,data={'source_dataset_id':did,'columns':ordered,'selected_column_ids':column_ids,'application_build':__import__('backend.build_info',fromlist=['build_info']).build_info()})
        s.add(record);s.flush();audit(s,did,'template_saved',{'template_id':template.id,'revision':revision});return obj(record)


def propose_copy(did,revision_id,execute=False,product_ids=None):
    with Session() as s:
        ds=require(s,Dataset,did);revision=require(s,TemplateRevision,revision_id)
        template=require(s,TagTemplate,revision.template_id)
        if template.workspace_id!=ds.workspace_id:raise ValueError('只能选择同项目的标签标准；请先确认项目归属')
        payload=copy.deepcopy(revision.data);name=template.name
    existing={c['key'] for c in draft.sheet(did)['columns']}
    refs={};changes=[];execute_refs=[]
    for c in payload['columns']:
        d=copy.deepcopy(c['definition']);key=d['key']
        if len(key)>40:key=key[:31]+'_'+hashlib.sha256(key.encode()).hexdigest()[:8]
        if key in existing:key=key[:26]+'_copy_'+uid()[:8]
        existing.add(key);refs[c['id']]='$'+key;d['key']=key
        d['depends_on']=[refs[x] for x in d.get('depends_on',[])]
        if d.get('parent_id'):d['parent_id']=refs[d['parent_id']]
        if d.get('when'):d['when']['column_id']=refs[d['when']['column_id']]
        d.pop('skill',None);d.pop('runtime_profile',None);d.pop('visual_standard_id',None);d.pop('visual_standard',None)
        changes.append({'kind':'add','definition':d,'template_source':{'template_id':revision.template_id,'revision':revision.revision,'revision_id':revision.id,'source_dataset_id':payload['source_dataset_id'],'source_column_id':c['id']}})
        if c['id'] in payload['selected_column_ids'] and not d.get('expanded'):execute_refs.append('$'+key)
    if execute:
        change={'kind':'execute','column_ids':execute_refs,'overwrite':False,'reuse_results':True}
        if product_ids is not None:change['product_ids']=product_ids
        changes.append(change)
    return draft.propose(did,'复制标签标准：'+name+'（修订'+str(revision.revision)+'）'+('并执行' if execute else ''),changes)
