"""Read current code through an isolated HTTP service; never calls a model."""
import argparse,hashlib,json,time
from pathlib import Path

import httpx,openpyxl

parser=argparse.ArgumentParser()
parser.add_argument('--base',default='http://127.0.0.1:8877')
parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
client=httpx.Client(base_url=args.base,timeout=180)

def get(path):
    response=client.get(path);response.raise_for_status();return response.json()

def post(path,data):
    response=client.post(path,json=data);response.raise_for_status();return response

started=time.perf_counter();health=get('/api/health');datasets=get('/api/datasets')
report={'health':health,'dataset_cases':[],'model_requests':0,'vision_requests':0}
for dataset in datasets:
    sheet=get(f"/api/drafts/{dataset['id']}/sheet")
    assert len(sheet['rows'])==dataset['count']
    assert sheet['dataset']['headers']==dataset['headers']
    profile=get(f"/api/drafts/{dataset['id']}/profile")
    assert profile['total']==dataset['count']
    assert [field['name'] for field in profile['fields']]==dataset['headers']
    selected=[column['id'] for column in sheet['columns'][:1]]
    response=post(f"/api/drafts/{dataset['id']}/export",{'columns':selected})
    exported=args.out/(dataset['id']+'.xlsx');exported.write_bytes(response.content)
    workbook=openpyxl.load_workbook(exported,data_only=False)
    result=workbook['Product_Result'];detail=workbook['AI_Detail']
    assert result.max_row==dataset['count']+1
    assert result.max_column==len(dataset['headers'])+len(selected)
    assert len(result._images)==dataset['image_count']
    assert detail.cell(1,1).value=='商品标识' and detail.cell(1,2).value=='标识来源'
    report['dataset_cases'].append({
        'dataset_id':dataset['id'],'name':dataset['name'],'source_filename':dataset.get('source_filename'),
        'source_sha256':dataset['sha256'],'sheet':dataset['sheet'],'rows':dataset['count'],
        'original_columns':len(dataset['headers']),'images':len(result._images),
        'profile_basis':profile['basis'],'text_status':profile['text_status'],
        'export':str(exported),'export_sha256':hashlib.sha256(exported.read_bytes()).hexdigest(),
    })

swim=next(dataset for dataset in datasets if dataset['count']==352)
sheet=get(f"/api/drafts/{swim['id']}/sheet")
zero_rows=[row for row in sheet['rows'] if row['raw'].get('SKU') in (0,'0')]
row90=next(row for row in sheet['rows'] if row['row']==90)
assert row90['raw']['SKU']==0
assert row90['anchor']==row90['raw'].get('父ASIN') and row90['identity_source']=='parent_asin'
assert len(zero_rows)==10 and all(row['identity_source']=='parent_asin' for row in zero_rows)
report['identity_case']={
    'row90':{key:row90[key] for key in ('row','anchor','identity_source')},
    'raw_sku':row90['raw']['SKU'],'parent_asin':row90['raw']['父ASIN'],
    'sku_zero_rows':len(zero_rows),'all_zero_rows_use_parent_asin':True,
}
assert health['capabilities']['visual_tagging'] is True
assert health['capabilities']['visual_tagging_status']=='available'
report['visual_capability']={
    'enabled':True,
    'status':'available',
    'boundary':'本脚本不发模型请求；自动视觉与图文联合能力由单独真实模型验收报告证明。',
}
report['seconds']=time.perf_counter()-started
path=args.out/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(path),'rows':[x['rows'] for x in report['dataset_cases']],
                  'columns':[x['original_columns'] for x in report['dataset_cases']],
                  'images':[x['images'] for x in report['dataset_cases']],
                  'identity_case':report['identity_case'],'visual_capability':report['visual_capability'],
                  'seconds':report['seconds']},ensure_ascii=False,indent=2))
