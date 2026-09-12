"""Exact, project-scoped reuse. A missing input never qualifies as a match."""
import copy,hashlib,json
from PIL import Image,ImageOps
from sqlalchemy import select
from .db import *


def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def pixels(path):
    with Image.open(path) as image:
        image=ImageOps.exif_transpose(image).convert('RGBA')
        return digest([image.size,hashlib.sha256(image.tobytes()).hexdigest()])


def fingerprint(product,column,spec,entry,vision_requirements):
    from .vision import provider_config
    from .execution_contract import require_contract
    execution=require_contract(spec)
    definition=copy.deepcopy(column['definition'])
    # Dependency identities differ between months; compare their definitions and frozen values.
    deps=spec['expected'][product['id']+':'+column['id']].get('dependencies',{})
    with Session() as s:
        names={cid:require(s,DraftColumn,cid).key for cid in deps}
    definition['depends_on']=[names.get(cid,cid) for cid in definition.get('depends_on',[])]
    if definition.get('parent_id'):definition['parent_id']=names.get(definition['parent_id'],definition['parent_id'])
    if definition.get('when'):definition['when']['column_id']=names.get(definition['when']['column_id'],definition['when']['column_id'])
    text_endpoint,_,text_model=provider_config(spec['text_model'])
    inputs={k:v for k,v in entry.items() if k not in ('product_id','visual','upstream')}
    if inputs.get('text_record'):
        record=inputs['text_record']
        inputs['text_record']={k:record.get(k) for k in ('content','source','status')}
    inputs['upstream']={names[cid]:{k:v for k,v in dep.items() if k in ('value','status')} for cid,dep in deps.items()}
    contract={'version':1,'definition':definition,'inputs':inputs,
              'text_model':[spec['text_model'],text_endpoint,text_model],
              'fill_prompt':execution['fill_prefix']}
    if definition['source'] in ('image','both'):
        endpoint,_,model=provider_config(spec['vision_model'])
        from .image_inputs import selection
        image_input=selection(product,definition.get('input_policy','single_main_image_v1'))
        contract.update(images=[pixels(asset['path']) for asset in image_input['assets']],image_input_policy=image_input['input_policy'],crop_policy=image_input.get('crop_policy'),vision_model=[spec['vision_model'],endpoint,model],
                        vision_prompt=execution['vision_rules'],vision_requirements=vision_requirements)
    return digest(contract)


def lookup(did,fp):
    with Session() as s:
        project=require(s,Dataset,did).workspace_id;candidates=[]
        for record in s.scalars(select(CellReuse).where(CellReuse.fingerprint==fp)):
            cell=require(s,Cell,record.cell_id);col=require(s,DraftColumn,cell.column_id)
            ds=require(s,Dataset,col.dataset_id)
            if ds.workspace_id!=project or cell.status not in ('ready','unknown') or cell.conflict or cell.revision!=col.revision:continue
            if cell.evidence.get('input_fingerprint')!=fp:continue
            candidates.append({'dataset_id':ds.id,'cell_id':cell.id,'column_id':col.id,'product_id':cell.product_id,
                'edit':cell.edit,'value':cell.value,'raw_value':cell.raw_value,
                'manual':cell.manual or cell.evidence.get('reuse_source',{}).get('manual'),
                'evidence':{k:v for k,v in cell.evidence.items() if k!='reuse_source'}})
        # Human final values take priority. Disagreement remains a review candidate.
        manual=[c for c in candidates if c['manual'] or c['evidence'].get('reuse_source',{}).get('manual')]
        chosen=manual or candidates
        if not chosen:return None
        if len({digest(c['value']) for c in chosen})>1:return {'conflicts':chosen}
        source=chosen[0]
        return {'normalized_value':source['value'],'raw_value':source['raw_value'],
                'evidence':'严格匹配必要输入、规则、Skill及模型配置后复用；原始证据见reuse_source',
                'reuse_source':source,'input_fingerprint':fp}


def index(s,cell):
    fp=cell.evidence.get('input_fingerprint')
    if not fp or cell.status not in ('ready','unknown'):return
    record=s.scalar(select(CellReuse).where(CellReuse.cell_id==cell.id))
    if record:record.fingerprint=fp
    else:s.add(CellReuse(cell_id=cell.id,fingerprint=fp))
