"""P0 F01: a raw table image column must not silently disable vision."""
import pytest
from sqlalchemy import select
from backend import draft,field_catalog,source_label_inference
from backend.db import *
from test_source_label_inference import seed_partial_labels

DISPIMG='=_xlfn.DISPIMG("ID_TEST_ONLY",1)'


def add_image_headers(did):
    with Session.begin() as session:
        dataset=session.get(Dataset,did)
        for header in ('图片','商品主图'):
            if header not in dataset.headers:dataset.headers=[*dataset.headers,header]
        for row in session.scalars(select(Product).where(Product.dataset_id==did)):
            row.raw={**row.raw,'图片':DISPIMG,'商品主图':'https://example.test/only.jpg'}
    field_catalog._inferred.pop(did,None)


def prepare(dataset,monkeypatch):
    did=dataset['id'];seed_partial_labels(did,total=10);add_image_headers(did)
    monkeypatch.setattr('backend.model_settings.defaults',lambda:('test/model','test/vision'))
    monkeypatch.setattr('backend.model_settings.require_validated',lambda *a,**k:True)
    return did


def test_raw_image_column_canonicalises_to_the_image_asset_field(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    options=source_label_inference.evidence_options(did)
    embedded=next(x for x in options['fields'] if x['embedded_image'])
    assert embedded['id']=='图片' and embedded['role']=='image'
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['图片']})
    assert item['evidence_fields']==['__image__']
    job=source_label_inference.analyze(did,item['id'],{'text_model':'test/model'})
    assert job['spec']['requires_visual'] is True
    draft=source_label_inference.read(did,item['id'])
    assert draft['last_estimate']['image_count']==10
    assert draft['last_estimate']['estimated_vision_requests']>0


def test_draft_stored_before_the_fix_is_normalized_on_analyze(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['图片']})
    with draft.WRITE_LOCK,Session.begin() as s:
        record=require(s,SourceLabelDraft,item['id']);record.data={**record.data,'evidence_fields':['图片']}
    job=source_label_inference.analyze(did,item['id'],{'text_model':'test/model'})
    assert job['spec']['requires_visual'] is True
    stored=source_label_inference.read(did,item['id'])
    assert stored['evidence_fields']==['__image__']
    assert stored['evidence_normalization']['raw']==['图片']


def test_image_url_column_is_never_treated_as_real_pixels(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['商品主图']})
    assert item['evidence_fields']==['商品主图']
    job=source_label_inference.analyze(did,item['id'],{'text_model':'test/model'})
    assert job['spec']['requires_visual'] is False
    assert source_label_inference.read(did,item['id'])['last_estimate']['image_count']==0


def test_legacy_image_sentinel_still_accepted(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['__image__']})
    assert item['evidence_fields']==['__image__']


def test_text_record_sentinel_is_untouched(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    item=source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['__text_record__','ASIN']})
    assert item['evidence_fields']==['__text_record__','ASIN']


def test_no_display_name_collision(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    options=source_label_inference.evidence_options(did)
    names=[x['name'] for x in options['fields']+options['special']]
    assert len(names)==len(set(names))
    assert '商品主图（已导入图片资产）' in names
    assert '图片（表格内嵌图片）' in names


def test_profile_warns_when_images_exist(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    profile=source_label_inference.profile(did,'上衣样式')
    assert profile['available_image_count']==10
    assert profile['image_evidence']['embedded_column']=='图片'
    assert '商品主图（已导入图片资产）' in profile['image_evidence']['notice']


def test_unknown_column_is_still_rejected(dataset,monkeypatch):
    did=prepare(dataset,monkeypatch)
    with pytest.raises(ValueError):
        source_label_inference.create(did,{'header':'上衣样式','mode':'infer','evidence_fields':['不存在的列']})
