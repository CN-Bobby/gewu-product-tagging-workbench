import math
from sqlalchemy import select
from backend import draft,source_label_inference
from backend.db import Session,Dataset,Product,DraftColumn,Cell


def seed_partial_labels(did,total=30):
    with Session.begin() as session:
        dataset=session.get(Dataset,did);dataset.headers=[*dataset.headers,'上衣样式']
        rows=list(session.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)))
        template=rows[-1]
        for number in range(len(rows)+1,total+1):
            session.add(Product(dataset_id=did,row=number+1,anchor=f'extra-{number}',identity_source='test',
                raw={**template.raw,'ASIN':f'EXTRA-{number}','上衣样式':None},fields={**template.fields,'asin':f'EXTRA-{number}'}))
        values=['U领运动','U领运动','V领吊带','三角杯','三角杯','斜肩上衣']
        for row,value in zip(rows,values):row.raw={**row.raw,'上衣样式':value}
        for row in rows[len(values):]:row.raw={**row.raw,'上衣样式':None}


def test_profile_extracts_human_values_and_initial_sample_includes_unlabeled(dataset):
    did=dataset['id'];seed_partial_labels(did)
    profile=source_label_inference.profile(did,'上衣样式')
    assert profile['labeled_count']==6 and profile['blank_count']==24
    assert {x['display'] for x in profile['human_values']}=={'U领运动','V领吊带','三角杯','斜肩上衣'}
    inferred=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['ASIN']})
    plan=inferred['sample_plan']
    assert len(plan['initial_product_ids'])==24
    assert plan['unlabeled_initial_count']>=math.ceil(len(plan['initial_product_ids'])/3)
    assert not plan['uncovered_human_value_ids']
    assert inferred['standard'] is None and inferred['status']=='profiled'


def test_manual_path_never_requires_retyping_values_and_protects_originals_on_confirm(dataset):
    did=dataset['id'];seed_partial_labels(did,total=10)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'manual','definition':'按商品资料判断上衣的主要结构样式','evidence_fields':['ASIN']})
    assert [x['value'] for x in item['standard']['observed_human_values']]==['U领运动','三角杯','V领吊带','斜肩上衣']
    with Session() as session:assert session.scalar(select(DraftColumn.id)) is None
    result=source_label_inference.confirm(did,item['id'],{})
    with Session() as session:
        column=session.get(DraftColumn,result['id']);cells=list(session.scalars(select(Cell).where(Cell.column_id==column.id)))
        products=list(session.scalars(select(Product).where(Product.dataset_id==did).order_by(Product.row)))
    assert column.definition['fill_empty_only'] is True and column.definition['source_header']=='上衣样式'
    assert column.definition['fields']==['ASIN']
    assert sum(cell.manual is not None for cell in cells)==6
    assert [row.raw.get('上衣样式') for row in products[:6]]==['U领运动','U领运动','V领吊带','三角杯','三角杯','斜肩上衣']


def test_questions_are_optional_and_answers_are_stored_without_confirming(dataset):
    did=dataset['id'];seed_partial_labels(did,total=10)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'manual','definition':'按商品资料判断上衣的主要结构样式','evidence_fields':['ASIN']})
    standard={**item['standard'],'questions':[{'question':'同款多结构时如何记录？','options':['单选主结构','允许多选']} ]}
    item=source_label_inference.update(did,item['id'],{'standard':standard})
    answered=source_label_inference.answer_questions(did,item['id'],[{'question_index':0,'answer':'允许多选'}])
    assert answered['question_answers']==[{'question_index':0,'question':'同款多结构时如何记录？','answer':'允许多选'}]
    assert answered['standard']['review_decisions']==answered['question_answers'] and answered['standard']['questions']==[]
    assert answered['status']=='review_required' and answered['confirmed_column_id'] is None


def test_already_adopted_source_can_reverse_infer_and_revise_without_duplicate(dataset):
    did=dataset['id'];seed_partial_labels(did,total=10)
    first=source_label_inference.create(did,{'header':'上衣样式','mode':'manual','definition':'按商品资料判断上衣的主要结构样式','evidence_fields':['ASIN']})
    adopted=source_label_inference.confirm(did,first['id'],{})
    profile=source_label_inference.profile(did,'上衣样式')
    assert profile['adopted']['column_id']==adopted['id'] and profile['adopted']['revision']==1
    second=source_label_inference.create(did,{'header':'上衣样式','mode':'manual','definition':'结合标题和图片判断上衣最主要的结构款式','evidence_fields':['商品标题','__image__']})
    revised=source_label_inference.confirm(did,second['id'],{})
    with Session() as session:
        columns=list(session.scalars(select(DraftColumn).where(DraftColumn.dataset_id==did)))
        cells=list(session.scalars(select(Cell).where(Cell.column_id==adopted['id'])))
    assert revised['id']==adopted['id'] and revised['mode']=='revised'
    assert len(columns)==1 and columns[0].revision==2
    assert columns[0].definition['definition']=='结合标题和图片判断上衣最主要的结构款式'
    assert columns[0].definition['inferred_standard']['target']==columns[0].definition['definition']
    assert sum(cell.manual is not None for cell in cells)==6
    assert [cell.value for cell in cells[:6]]==['U领运动','U领运动','V领吊带','三角杯','三角杯','斜肩上衣']
