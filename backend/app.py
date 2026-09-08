import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select
from .db import *
from . import importer, service, jobs, harness
from .exporter import export_run
from . import draft,batch,conversation,draft_export,text_facts
from . import field_catalog,worksheet_query,task_control
from . import knowledge,templates,storage_management,model_settings
from .build_info import VERSION, build_info

@asynccontextmanager
async def lifespan(app):
    app.state.stopping=False
    init_db();jobs.recover();batch.recover()
    with Session() as s:ids=list(s.scalars(select(Dataset.id)))
    for did in ids:draft.migrate(did)
    yield
    for rid in list(jobs.active):jobs.stop_run(rid)
    for did in list(jobs.chats):jobs.stop_chat(did)
    for jid in list(batch.active):batch.stop(jid)
    conversation.close()

app=FastAPI(title='商品特征工程工作台',version=VERSION,lifespan=lifespan)

@app.middleware('http')
async def localhost(request:Request,call_next):
    host=request.url.hostname
    if host not in ('127.0.0.1','localhost','testserver'):return JSONResponse({'detail':'本工具仅用于本机'},status_code=403)
    origin=request.headers.get('origin')
    if origin and urlparse(origin).netloc!=request.headers.get('host'):return JSONResponse({'detail':'不接受跨站请求'},status_code=403)
    return await call_next(request)

@app.exception_handler(ValueError)
async def value_error(request,e):return JSONResponse({'detail':str(e)},status_code=400)

@app.exception_handler(ValidationError)
async def validation_error(request,e):return JSONResponse({'detail':str(e)},status_code=422)

@app.get('/api/health')
def health():return {'status':'ok','application':'product-tagging-workbench','storage':str(STORAGE),**build_info()}

@app.get('/api/settings/models')
def settings_models():return {**model_settings.read(),'defaults':model_settings.defaults()}

@app.post('/api/settings/models')
def settings_models_save(body:dict):return model_settings.save(body)

@app.get('/api/storage')
def storage_usage():return storage_management.usage()

@app.post('/api/storage/clear-cache')
def cache_clear():return storage_management.clear()

@app.get('/api/drafts/{did}/fields')
def fields_read(did:str):return field_catalog.read(did)

@app.post('/api/drafts/{did}/fields/suggest')
def fields_suggest(did:str):return field_catalog.suggest(did)

@app.post('/api/drafts/{did}/fields/confirm')
def fields_confirm(did:str,body:dict):return field_catalog.confirm(did,body['fields'],body['revision'])

@app.post('/api/drafts/{did}/query')
def query_rows(did:str,body:dict):return worksheet_query.query(did,body.get('filters'),body.get('search',''),body.get('facet'),body.get('save',False),body.get('job_id'))

@app.get('/api/drafts/{did}/profile')
def data_profile(did:str):return worksheet_query.profile(did)

@app.post('/api/drafts/{did}/profile/observe')
def data_observe(did:str,body:dict):return worksheet_query.visual_overview(did,body.get('goal',''),body.get('limit',20))

@app.get('/api/drafts/{did}/knowledge')
def knowledge_read(did:str):return knowledge.read(did)

@app.post('/api/drafts/{did}/knowledge')
def knowledge_research(did:str,body:dict):return knowledge.research(did,body.get('query',''),body.get('urls'),body.get('scope',''),body.get('refresh',False))

@app.patch('/api/drafts/{did}/knowledge/{ident}')
def knowledge_review(did:str,ident:str,body:dict):return knowledge.review(did,ident,body.get('correction',''),body.get('confirmed',False))

@app.get('/api/jobs/{jid}')
def job_detail(jid:str):return task_control.detail(jid)

@app.post('/api/drafts/{did}/jobs/{jid}/repair')
def job_repair(did:str,jid:str,body:dict):return task_control.repair(did,jid,body.get('tokens'),body.get('definitions'),body.get('refresh_images',False))

@app.post('/api/drafts/{did}/jobs/{jid}/disposition')
def job_disposition(did:str,jid:str,body:dict):return task_control.disposition(did,jid,body['action'])

@app.post('/api/server/stop')
async def stop_server():
    import asyncio,signal
    app.state.stopping=True
    asyncio.get_running_loop().call_later(.2,lambda:signal.raise_signal(signal.SIGINT))
    return {'stopping':True}

@app.get('/api/harness')
def harness_status():return harness.status()

@app.get('/api/datasets')
def datasets():return service.list_datasets()

@app.get('/api/projects')
def project_list():return templates.projects()

@app.post('/api/projects')
def project_create(body:dict):return templates.create_project(body.get('name'))

@app.get('/api/projects/{wid}/templates')
def template_list(wid:str):return templates.list_templates(wid)

@app.post('/api/drafts/{did}/templates')
def template_save(did:str,body:dict):return templates.save(did,body.get('name'),body.get('columns'),body.get('template_id'))

@app.post('/api/drafts/{did}/templates/copy')
def template_copy(did:str,body:dict):return templates.propose_copy(did,body['revision_id'],body.get('execute',False),body.get('product_ids'))

async def upload_bytes(file):
    if not (file.filename or '').lower().endswith('.xlsx'):raise ValueError('请上传 .xlsx 文件')
    data=await file.read(100*1024*1024+1)
    if len(data)>100*1024*1024:raise ValueError('文件超过 100 MB')
    return data

@app.post('/api/import/inspect')
async def inspect(file:UploadFile=File(...),sheet:str=Form('')):return importer.inspect(await upload_bytes(file),sheet or None)

@app.post('/api/import')
async def import_data(file:UploadFile=File(...),sheet:str=Form(''),workspace_id:str=Form(''),mapping:str=Form('{}')):
    dataset=importer.import_file(await upload_bytes(file),file.filename,sheet or None,workspace_id or None,json.loads(mapping))
    task=text_facts.prepare(dataset['id'])
    if task['status']=='queued':batch.launch(task['id'])
    return {**dataset,'text_job_id':task['id'],'profile':worksheet_query.profile(dataset['id'])}

@app.get('/api/drafts/{did}/text-records')
def text_records(did:str,offset:int=0,limit:int=50):return text_facts.records(did,offset,limit)

@app.post('/api/drafts/{did}/text-records/extract')
def text_extract(did:str,body:dict):
    task=text_facts.prepare(did,body.get('text_model'))
    if task['status']=='queued' and task['id'] not in batch.active:batch.launch(task['id'])
    return task

@app.get('/api/datasets/{dataset_id}')
def state(dataset_id:str):return service.state(dataset_id)

@app.get('/api/datasets/{dataset_id}/products')
def products(dataset_id:str,offset:int=0,limit:int=50):return service.products(dataset_id,max(0,offset),max(1,limit))

@app.post('/api/datasets/{dataset_id}/images')
async def images(dataset_id:str,files:list[UploadFile]=File(...)):
    inputs=[]
    for f in files:
        data=await f.read(25*1024*1024+1)
        if len(data)>25*1024*1024:raise ValueError('单张图片超过 25 MB')
        inputs.append((f.filename,data))
    return importer.attach_images(dataset_id,inputs)

@app.get('/api/assets/{asset_id}')
def asset(asset_id:str):
    with Session() as s:
        a=require(s,Asset,asset_id)
        if not a.path:raise ValueError('外部图片尚未缓存')
        return FileResponse(a.path)

@app.post('/api/datasets/{dataset_id}/chat')
def chat(dataset_id:str,body:dict):return conversation.start(dataset_id,body.get('text',''),body.get('model') or model_settings.defaults()[0],body.get('proposal_id'),body.get('vision_model'))

@app.post('/api/datasets/{dataset_id}/chat/stop')
def stop_chat(dataset_id:str):return conversation.stop(dataset_id)

@app.post('/api/datasets/{dataset_id}/schemas')
def propose(dataset_id:str,body:dict):return service.propose(dataset_id,body.get('goal',''),body.get('definitions',[]))

@app.post('/api/schemas/{schema_id}/approve')
def approve(schema_id:str):return service.approve_schema(schema_id)

@app.post('/api/datasets/{dataset_id}/runs')
def start_run(dataset_id:str,body:dict):
    raise ValueError('旧版逐商品 Agent 执行入口已停用，请刷新页面，在持续草稿中安排打标')

@app.get('/api/runs/{run_id}/results')
def results(run_id:str):return service.run_results(run_id)

@app.get('/api/runs/{run_id}/distribution')
def distribution(run_id:str):return service.distribution(run_id)

@app.post('/api/runs/{run_id}/stop')
def stop(run_id:str):return jobs.stop_run(run_id)

@app.post('/api/runs/{run_id}/resume')
def resume(run_id:str):raise ValueError('旧任务仅保留查询，请在持续草稿中安排未完成标签')

@app.post('/api/tags/{tag_id}/review')
def review(tag_id:str,body:dict):return service.review_tag(tag_id,body.get('values'),body.get('note',''))

@app.post('/api/runs/{run_id}/approve')
def approve_sample(run_id:str):return service.approve_sample(run_id)

@app.post('/api/runs/{run_id}/transforms')
def transform(run_id:str,body:dict):return service.propose_transform(run_id,body['source_key'],body['output_key'],body['definition'],body['mapping'],body.get('type','INTERPRETATION'))

@app.post('/api/transforms/{ident}/approve')
def approve_transform(ident:str):return service.approve_transform(ident)

@app.get('/api/runs/{run_id}/export')
def export(run_id:str):return FileResponse(export_run(run_id),filename='商品打标结果.xlsx',media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get('/api/experiences')
def experiences():
    with Session() as s:return [obj(x) for x in s.scalars(select(Experience).order_by(Experience.created_at.desc()))]

@app.get('/api/drafts/{did}')
def draft_state(did:str):return draft.state(did)

@app.get('/api/drafts/{did}/sheet')
def draft_sheet(did:str,version:str|None=None):return draft.sheet(did,version)

@app.post('/api/drafts/{did}/proposals')
def draft_propose(did:str,body:dict):return draft.propose(did,body.get('title','变更提案'),body['changes'])

@app.post('/api/drafts/{did}/proposals/{pid}/confirm')
def draft_confirm(did:str,pid:str,body:dict):
    result=draft.confirm(did,pid,body.get('product_ids'),body.get('sample',False),body.get('text_model'),body.get('vision_model'))
    for j in result:batch.launch(j['id'])
    return result

@app.post('/api/drafts/{did}/proposals/{pid}/reject')
def draft_reject(did:str,pid:str):
    with draft.WRITE_LOCK,Session.begin() as s:
        p=require(s,Proposal,pid)
        if p.dataset_id!=did or p.status!='pending':raise ValueError('提案不可撤回')
        p.status='rejected';draft.emit(s,did,'proposal',obj(p));return obj(p)

@app.patch('/api/drafts/{did}/cells/{product_id}/{column_id}')
def draft_correct(did:str,product_id:str,column_id:str,body:dict):return draft.correct(did,product_id,column_id,body.get('value'),body.get('expected_edit'))

@app.post('/api/drafts/{did}/versions')
def draft_version(did:str):return draft.save_version(did)

@app.get('/api/drafts/{did}/compare')
def draft_compare(did:str,left:str,right:str|None=None):return draft.compare(did,left,right)

@app.post('/api/jobs/{jid}/stop')
def batch_stop(jid:str):return batch.stop(jid)

@app.post('/api/jobs/{jid}/resume')
def batch_resume(jid:str):batch.resume(jid);return {'resuming':True}

@app.post('/api/jobs/{jid}/cancel')
def batch_cancel(jid:str):return batch.stop(jid,cancel=True)

@app.post('/api/drafts/{did}/export')
def draft_download(did:str,body:dict):return FileResponse(draft_export.export(did,body.get('columns',[]),body.get('version')),filename='商品打标结果.xlsx')

@app.get('/api/drafts/{did}/events')
async def draft_events(did:str,request:Request,after:int=0):
    import asyncio
    from fastapi.responses import StreamingResponse
    async def events():
        cursor=max(after,int(request.headers.get('last-event-id','0')))
        while not getattr(app.state,'stopping',False) and not await request.is_disconnected():
            with Session() as s:items=[obj(x) for x in s.scalars(select(DraftEvent).where(DraftEvent.dataset_id==did,DraftEvent.id>cursor).order_by(DraftEvent.id).limit(200))]
            for item in items:
                cursor=item['id'];yield f'id: {cursor}\ndata: '+json.dumps(item,ensure_ascii=False)+'\n\n'
            if not items:yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

dist=Path(os.environ.get('TAG_FRONTEND_DIST',ROOT/'frontend'/'dist'))
if dist.exists():app.mount('/',StaticFiles(directory=dist,html=True),name='frontend')
