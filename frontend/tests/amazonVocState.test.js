import test from 'node:test';
import assert from 'node:assert/strict';
import {
  AMAZON_VOC_API,amazonVocEndpoints,buildAmazonVocRunForm,
  hasAmazonVocArtifacts,isAmazonVocActive,
} from '../src/amazonVocState.js';

test('VOC API contract is namespaced and safely encodes resource identifiers',()=>{
  assert.equal(AMAZON_VOC_API,'/api/amazon-voc');
  assert.equal(amazonVocEndpoints.run('run/a b'),'/api/amazon-voc/runs/run%2Fa%20b');
  assert.equal(amazonVocEndpoints.artifact('run-1','excel'),'/api/amazon-voc/runs/run-1/artifacts/excel');
  assert.equal(amazonVocEndpoints.runs('project 1'),'/api/amazon-voc/runs?project_id=project%201');
});

test('upload request contains exactly one review workbook plus task settings',()=>{
  const file=new Blob(['fixture'],{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
  file.name='reviews.xlsx';
  const form=buildAmazonVocRunForm({file,model:'shared/text',credentialId:'key-1'});
  assert.deepEqual([...form.keys()],['reviews','model','credential_id','force_refresh']);
  assert.equal(form.get('force_refresh'),'false');
  const forced=buildAmazonVocRunForm({file,model:'shared/text',credentialId:'key-1',forceRefresh:true});
  assert.equal(forced.get('force_refresh'),'true');
  assert.equal(form.get('reviews').name,'reviews.xlsx');
  assert.throws(()=>buildAmazonVocRunForm({file:{name:'reviews.csv'}}),/仅支持/);
  assert.throws(()=>buildAmazonVocRunForm({}),/请选择/);
});

test('progress and report links are exposed only for a completed report bundle',()=>{
  assert.equal(isAmazonVocActive('extracting'),true);
  assert.equal(isAmazonVocActive('completed'),false);
  assert.equal(hasAmazonVocArtifacts({status:'completed',artifacts:['html','excel']}),true);
  assert.equal(hasAmazonVocArtifacts({status:'completed',artifacts:['html']}),false);
  assert.equal(hasAmazonVocArtifacts({status:'failed',artifacts:['html','excel']}),false);
});
