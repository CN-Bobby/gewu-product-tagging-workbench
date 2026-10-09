import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select
from .db import *
from . import importer, service, jobs, harness
from .exporter import export_run
from . import draft,batch,draft_export,text_facts
from . import field_catalog,worksheet_query,task_control,project_files,project_library
from . import knowledge,templates,storage_management,model_settings,capabilities
from .keyword_research import routes as keyword_research_routes
from .keyword_research import registry as keyword_registry, runner as keyword_runner
from . import amazon_scraper
from .amazon_reviews import service as amazon_reviews
from .amazon_voc.router import router as amazon_voc_router, recover as amazon_voc_recover, stop_all as amazon_voc_stop_all
from .product_archive import router as product_archive_router
from .amazon_scraper_http import (AmazonRequestBodyTooLarge, InvalidAmazonContentLength,
                                  buffer_amazon_request_body)
from .xunlongjue import router as xunlongjue_router, home_summary as xunlongjue_home_summary
from .xunlongjue import start as xunlongjue_start, recover as xunlongjue_recover, stop_all as xunlongjue_stop_all
from .build_info import VERSION, build_info
from .agent_http import router as agent_router
from . import agent_application
from .model_settings import SettingsInUse
from .runtime_isolation import CredentialUnavailable
from . import external_effects

AMAZON_REQUEST_BODY_LIMIT = 256 * 1024

@asynccontextmanager
async def lifespan(app):
    app.state.stopping=False
    init_db()
    # DEV and TEST are offline by default and may opt in only for this process. Persisted
    # queued/interrupted tasks must never resume merely because one of these environments restarted.
    # Persisted work is recovered only by the real production profile. Test harnesses may
    # explicitly exercise mocked provider paths, but that must never trigger queued-job recovery.
    if external_effects.profile() == 'prod':
        agent_application.start()
        jobs.recover();batch.recover();keyword_registry.recover();amazon_scraper.recover()
        amazon_reviews.recover();amazon_voc_recover();xunlongjue_start();xunlongjue_recover()
    with Session() as s:ids=list(s.scalars(select(Dataset.id)))
    for did in ids:draft.migrate(did)
    yield
    # Interactive sessions first: closing them stops a turn from starting work while the rest of
    # the application is already going away. Each close is a normal stop, not a forced teardown.
    agent_application.shutdown()
    for rid in list(jobs.active):jobs.stop_run(rid)
    for did in list(jobs.chats):jobs.stop_chat(did)
    for jid in list(batch.active):batch.stop(jid)
    keyword_runner.stop_all()
    amazon_scraper.stop_all()
    amazon_reviews.stop_all()
    amazon_voc_stop_all()
    xunlongjue_stop_all()
    from .runtime_isolation import clear_ephemeral_credentials
    clear_ephemeral_credentials()

app=FastAPI(title='商品特征工程工作台',version=VERSION,lifespan=lifespan)
app.include_router(amazon_voc_router)
app.include_router(product_archive_router)

def _external_service_for_request(method: str, path: str, body: bytes = b'') -> str | None:
    """Classify writes that may start a paid or external task; previews and cancellation stay local."""
    if method not in {'POST', 'PUT', 'PATCH'}:
        return None
    path = path.rstrip('/') or '/'
    if path.startswith('/api/agent/sessions'):
        if path.endswith(('/cancel', '/stop')):
            return None
        return 'model_api'
    if path.startswith('/api/amazon-scraper/jobs'):
        return None if path.endswith('/cancel') else 'amazon_collection'
    if path.startswith('/api/amazon-reviews/jobs'):
        return None if path.endswith('/cancel') else 'amazon_reviews_or_translation'
    if path.startswith('/api/amazon-reviews/from-product-archive/'):
        return 'amazon_reviews_or_translation'
    if path.startswith('/api/amazon-voc/'):
        if path.endswith('/cancel') or (method == 'PATCH' and '/events/' in path):
            return None
        if path.endswith('/projects') and method == 'POST':
            return None
        return 'amazon_voc_model'
    if path.startswith('/api/keyword-research/'):
        if path.endswith(('/plan', '/runs', '/queue/continue')) or (method == 'POST' and path.endswith('/queue')):
            return 'keyword_mcp_and_model'
    if path.startswith('/api/xunlongjue/'):
        if path.endswith(('/runs', '/runs/bulk')) or path.endswith('/resume'):
            return 'sorftime_mcp_and_model'
    if path.startswith('/api/drafts/'):
        if path.endswith('/knowledge'):
            return 'external_research'
        if any(path.endswith(suffix) for suffix in ('/fields/suggest', '/profile/observe', '/text-records/extract')):
            return 'model_api_or_mcp'
        if '/source-label-drafts/' in path and path.endswith(('/analyze', '/expand')):
            return 'model_api'
        if '/jobs/' in path and path.endswith('/repair'):
            return 'model_api'
        if '/repair-scopes/' in path and path.endswith('/propose'):
            return 'model_api'
        if path.endswith('/templates/copy'):
            try:
                payload = json.loads(body or b'{}')
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            if payload.get('execute'):
                return 'model_api'
    if path.startswith('/api/jobs/') and path.endswith('/resume'):
        return 'model_api'
    if path.startswith('/api/datasets/') and path.endswith('/images'):
        return 'amazon_collection'
    if path.startswith('/api/runs/') and path.endswith('/resume'):
        return 'model_api'
    return None

@app.middleware('http')
async def localhost(request:Request,call_next):
    host=request.url.hostname
    if host not in ('127.0.0.1','localhost','testserver'):return JSONResponse({'detail':'本工具仅用于本机'},status_code=403)
    origin=request.headers.get('origin')
    if origin and urlparse(origin).netloc!=request.headers.get('host'):return JSONResponse({'detail':'不接受跨站请求'},status_code=403)
    request_body = await request.body() if request.url.path.endswith('/templates/copy') else b''
    service_name = _external_service_for_request(request.method, request.url.path, request_body)
    if service_name:
        try:
            external_effects.assert_allowed(service_name)
        except external_effects.ExternalEffectsDisabled as exc:
            return JSONResponse({'detail':{'code':exc.code,'message':str(exc)}},status_code=403)
    if request.method=='POST' and request.url.path.startswith('/api/amazon-scraper/'):
        try:
            await buffer_amazon_request_body(request,AMAZON_REQUEST_BODY_LIMIT)
        except InvalidAmazonContentLength:
            return JSONResponse({'detail':'请求大小无效'},status_code=400)
        except AmazonRequestBodyTooLarge:
            return JSONResponse({'detail':'Amazon 请求内容过大：最多 256 KiB'},status_code=413)
    return await call_next(request)

@app.exception_handler(ValueError)
async def value_error(request,e):return JSONResponse({'detail':str(e)},status_code=400)

# Both of these are ValueErrors raised by the settings layer, so they are registered explicitly to
# carry a machine-readable code. A refusal is a conflict, not a malformed request: the request was
# well formed, the moment was not.
@app.exception_handler(SettingsInUse)
async def settings_in_use(request,e):
    return JSONResponse({'detail':{'code':'settings_in_use','message':str(e)}},status_code=409)

@app.exception_handler(CredentialUnavailable)
async def credential_unavailable(request,e):
    return JSONResponse({'detail':{'code':'credential_unavailable','message':str(e)}},status_code=400)

@app.exception_handler(external_effects.ExternalEffectsDisabled)
async def external_effects_disabled(request,e):
    return JSONResponse({'detail':{'code':e.code,'message':str(e)}},status_code=403)

@app.exception_handler(ValidationError)
async def validation_error(request,e):return JSONResponse({'detail':str(e)},status_code=422)

@app.get('/api/health')
def health():return {'status':'ok','application':'product-tagging-workbench','storage':str(STORAGE),'capabilities':capabilities.public(),**build_info()}

@app.get('/api/runtime/status')
def runtime_status(request:Request):
    """Read-only runtime identity and policy; secret values are never returned."""
    import hashlib
    info=build_info()
    try:
        from .credential_vault import credential_status
        credentials=credential_status(profile=external_effects.profile())
    except (ImportError,OSError,ValueError,RuntimeError):
        credentials={'vault':'unavailable','model':False,'sellersprite':False,'sorftime':False}
    reports_dir=Path(os.environ.get('GEWU_REPORTS_DIR',str(STORAGE/'reports'))).resolve()
    report=None
    if reports_dir.is_dir():
        candidates=sorted(reports_dir.glob('*.json'),key=lambda item:item.stat().st_mtime,reverse=True)
        for candidate in candidates:
            try:
                raw=candidate.read_bytes();data=json.loads(raw.decode('utf-8-sig'))
                report={'name':candidate.name,'sha256':hashlib.sha256(raw).hexdigest(),
                    'status':data.get('status') or data.get('result') or 'recorded',
                    'build_id':data.get('build_id'),'commit':data.get('commit'),
                    'created_at':data.get('created_at') or data.get('finished_at'),
                    'fixture_version':data.get('fixture_version')}
                break
            except (OSError,json.JSONDecodeError):
                continue
    backend_url=os.environ.get('GEWU_API_URL') or str(request.base_url).rstrip('/')
    frontend_url=os.environ.get('GEWU_FRONTEND_URL') or request.headers.get('origin')
    return {'profile':external_effects.profile(),'instance_id':os.environ.get('GEWU_INSTANCE_ID',''),
        'addresses':{'frontend':frontend_url,'backend':backend_url},
        'source':{'version':info.get('version',VERSION),'build_id':info.get('build_id'),
            'commit':info.get('commit'),'branch':info.get('branch'),'dirty':info.get('dirty'),
            'source_sha256':info.get('source_sha256'),'manifest_sha256':info.get('manifest_sha256'),
            'release_status':info.get('release_status'),'build_mode':info.get('build_mode')},
        'data':{'path':str(STORAGE),'identity':hashlib.sha256(os.path.normcase(str(STORAGE)).encode('utf-8')).hexdigest()[:12]},
        'credentials':credentials,'external_effects':external_effects.public_status(),
        'latest_test_report':report}

@app.get('/api/amazon-scraper/runtime')
def amazon_scraper_runtime():return amazon_scraper.runtime_status()

@app.post('/api/amazon-scraper/preview')
def amazon_scraper_preview(body:dict):return amazon_scraper.service.preview(body.get('input',''),body.get('marketplace','US'),body.get('profile','balanced'))

@app.post('/api/amazon-scraper/jobs')
def amazon_scraper_start(body:dict):return amazon_scraper.service.start(body.get('input',''),body.get('marketplace','US'),body.get('profile','balanced'))

@app.get('/api/amazon-scraper/jobs')
def amazon_scraper_jobs():return amazon_scraper.service.list_jobs()

@app.get('/api/amazon-scraper/jobs/{job_id}')
def amazon_scraper_job(job_id:str):return amazon_scraper.service.get(job_id)

@app.post('/api/amazon-scraper/jobs/{job_id}/cancel')
def amazon_scraper_cancel(job_id:str):return amazon_scraper.service.cancel(job_id)

@app.post('/api/amazon-scraper/jobs/{job_id}/retry')
def amazon_scraper_retry(job_id:str):return amazon_scraper.service.retry(job_id)

@app.get('/api/amazon-scraper/jobs/{job_id}/download')
def amazon_scraper_download(job_id:str):return FileResponse(amazon_scraper.service.output_path(job_id),filename='Amazon-Listing-图片.xlsx',media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get('/api/amazon-reviews/settings')
def amazon_reviews_settings():return amazon_reviews.model_settings_public()

@app.post('/api/amazon-reviews/jobs')
def amazon_reviews_start(body:dict):return amazon_reviews.create(body)

@app.post('/api/amazon-reviews/from-product-archive/{archive_id}')
def amazon_reviews_from_archive(archive_id:str,body:dict):
    from .product_archive import service as archive_service
    body=body or {}
    targets=archive_service.dispatch_targets(archive_id,body.get('product_ids') or [],
                                              child_only=bool(body.get('child_only',False)))
    requested_workspace=str(body.get('workspace_id') or targets.get('workspace_id') or '')
    if requested_workspace!=str(targets.get('workspace_id') or ''):
        raise ValueError('档案和选中行不属于同一项目')
    archive=archive_service._read(archive_id)
    return amazon_reviews.create({'mode':'asins','site':archive.get('marketplace') or 'US',
        'asins':targets['asins'],'model':body.get('model'),'credential_id':body.get('credential_id'),
        'workspace_id':targets.get('workspace_id'),'source_dataset_id':targets.get('dataset_id'),
        'source_archive_id':archive_id,'auto_voc_policy':body.get('auto_voc_policy','none')})

@app.get('/api/amazon-reviews/jobs')
def amazon_reviews_jobs():return amazon_reviews.list_jobs()

@app.get('/api/amazon-reviews/jobs/{job_id}')
def amazon_reviews_job(job_id:str):return amazon_reviews.get(job_id)

@app.post('/api/amazon-reviews/jobs/{job_id}/cancel')
def amazon_reviews_cancel(job_id:str):return amazon_reviews.cancel(job_id)

@app.post('/api/amazon-reviews/jobs/{job_id}/retry')
def amazon_reviews_retry(job_id:str):return amazon_reviews.retry(job_id)

@app.get('/api/amazon-reviews/jobs/{job_id}/download')
def amazon_reviews_download(job_id:str):return FileResponse(amazon_reviews.output_path(job_id),filename='Amazon-Reviews.xlsx',media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.get('/api/amazon-reviews/jobs/{job_id}/raw')
def amazon_reviews_raw(job_id:str):return FileResponse(amazon_reviews.raw_path(job_id),filename='Amazon-Reviews-raw.json',media_type='application/json')

@app.get('/api/home/summary')
def home_summary():
    """Small landing-page snapshot; configuration summaries never include secret values."""
    settings=model_settings.read()
    providers=[]
    for provider in settings.get('providers',[]):
        providers.append({'name':provider.get('name'),'models':provider.get('model_refs',[]),
            'credentials':[{'id':item.get('id'),'name':item.get('name'),
                'enabled':item.get('enabled',True),'authorized':item.get('authorized',False)}
                for item in provider.get('credentials',[])]})
    with Session() as s:
        tagging=[]
        for job in s.scalars(select(BatchJob).order_by(BatchJob.created_at.desc()).limit(5)):
            dataset=s.get(Dataset,job.dataset_id)
            tagging.append({'id':job.id,'dataset_id':job.dataset_id,
                'dataset_name':dataset.name if dataset else '', 'status':job.status,
                'created_at':job.created_at,'kind':(job.spec or {}).get('kind','labels')})
        recent_runs=[]
        for run in s.scalars(select(Run).order_by(Run.created_at.desc()).limit(5)):
            dataset=s.get(Dataset,run.dataset_id)
            recent_runs.append({'id':run.id,'dataset_id':run.dataset_id,
                'dataset_name':dataset.name if dataset else '', 'status':run.status,
                'created_at':run.created_at,'kind':run.kind})
    keywords=[]
    for item in keyword_registry.list_runs(limit=5)[0]:
        summary=keyword_registry.public_summary(item)
        keywords.append({'run_id':item['run_id'],'display_name':item.get('display_name') or '',
            'status':item.get('status'),'stage':item.get('stage'),'created_at':item.get('created_at'),
            'input_summary':summary['input_summary'],'budget':summary['budget']})
    xunlongjue=xunlongjue_home_summary()
    return {'configuration':{'models_configured':bool(settings.get('configured')),
        'default_model':settings.get('default_model'),'providers':providers,
        'sellersprite':keyword_research_routes.credentials.status(),
        'xunlongjue':{'key_configured':xunlongjue['key_configured']}},
        'recent_tagging_tasks':tagging,'recent_keyword_tasks':keywords,
        'recent_xunlongjue_runs':xunlongjue['recent_runs'],
        'recent_tagging_runs':recent_runs}

@app.get('/api/settings/models')
def settings_models():return {**model_settings.read(),'defaults':model_settings.defaults()}

@app.post('/api/settings/models')
def settings_models_save(body:dict):return model_settings.save(body)

@app.post('/api/settings/models/{provider}/credentials/{credential_id}/{action}')
def settings_credential_action(provider:str,credential_id:str,action:str):return model_settings.credential_action(provider,credential_id,action)

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

@app.post('/api/drafts/{did}/source-labels')
def adopt_source_label(did:str,body:dict):
    from .source_labels import adopt
    return adopt(did,body)

@app.get('/api/drafts/{did}/source-label-profile')
def source_label_profile(did:str,header:str):
    from .source_label_inference import profile
    return profile(did,header)

@app.get('/api/drafts/{did}/source-label-drafts')
def source_label_drafts(did:str):
    from .source_label_inference import listing
    return listing(did)

@app.post('/api/drafts/{did}/source-label-drafts')
def source_label_draft_create(did:str,body:dict):
    from .source_label_inference import create
    return create(did,body)

@app.get('/api/drafts/{did}/source-label-drafts/{ident}')
def source_label_draft_read(did:str,ident:str):
    from .source_label_inference import read
    return read(did,ident)

@app.patch('/api/drafts/{did}/source-label-drafts/{ident}')
def source_label_draft_update(did:str,ident:str,body:dict):
    from .source_label_inference import update
    return update(did,ident,body)

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/analyze')
def source_label_draft_analyze(did:str,ident:str,body:dict):
    from .source_label_inference import analyze
    job=analyze(did,ident,body);batch.launch(job['id']);return job

@app.get('/api/drafts/{did}/source-label-drafts/{ident}/expansion-estimate')
def source_label_draft_expansion_estimate(did:str,ident:str,target_limit:int=60):
    from .source_label_inference import expansion_estimate
    return expansion_estimate(did,ident,target_limit)

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/expand')
def source_label_draft_expand(did:str,ident:str,body:dict):
    from .source_label_inference import analyze
    job=analyze(did,ident,body,expansion=True);batch.launch(job['id']);return job

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/recovery')
def source_label_draft_recovery(did:str,ident:str,body:dict):
    from .source_label_inference import recover_from_attempt
    return recover_from_attempt(did,ident,body.get('attempt_id'))

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/baseline')
def source_label_draft_baseline(did:str,ident:str,body:dict):
    from .source_label_inference import program_baseline
    return program_baseline(did,ident)

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/answers')
def source_label_draft_answers(did:str,ident:str,body:dict):
    from .source_label_inference import answer_questions
    return answer_questions(did,ident,body.get('answers') or [])

@app.post('/api/drafts/{did}/source-label-drafts/{ident}/confirm')
def source_label_draft_confirm(did:str,ident:str,body:dict):
    from .source_label_inference import confirm
    return confirm(did,ident,body)

@app.post('/api/drafts/{did}/source-labels/{column_id}')
def source_label_status(did:str,column_id:str,body:dict):
    from .source_labels import set_active
    return set_active(did,column_id,body.get('active'))

@app.get('/api/drafts/{did}/profile')
def data_profile(did:str):return worksheet_query.profile(did)

@app.post('/api/drafts/{did}/profile/observe')
def data_observe(did:str,body:dict):
    capabilities.require_visual_tagging()
    return worksheet_query.visual_overview(did,body.get('goal',''),body.get('limit',24))

@app.get('/api/drafts/{did}/knowledge')
def knowledge_read(did:str):return knowledge.read(did)

@app.post('/api/drafts/{did}/knowledge')
def knowledge_research(did:str,body:dict):return knowledge.research(did,body.get('query',''),body.get('urls'),body.get('scope',''),body.get('refresh',False))

@app.patch('/api/drafts/{did}/knowledge/{ident}')
def knowledge_review(did:str,ident:str,body:dict):return knowledge.review(did,ident,body.get('correction',''),body.get('confirmed',False))

@app.get('/api/jobs/{jid}')
def job_detail(jid:str):return task_control.detail(jid)

@app.post('/api/drafts/{did}/jobs/{jid}/repair')
def job_repair(did:str,jid:str,body:dict):return task_control.repair(did,jid,body.get('tokens'),body.get('definitions'),body.get('refresh_images',False),body.get('repair_scope_id'))

@app.post('/api/drafts/{did}/repair-scopes')
def repair_scope_create(did:str,body:dict):return task_control.create_repair_scope(did,body.get('job_ids'),body.get('tokens'))

@app.get('/api/drafts/{did}/repair-scopes/{rid}')
def repair_scope_read(did:str,rid:str):return task_control.get_repair_scope(did,rid)

@app.post('/api/drafts/{did}/repair-scopes/{rid}/propose')
def repair_scope_propose(did:str,rid:str,body:dict):return task_control.repair(did,definitions=body.get('definitions'),refresh_images=body.get('refresh_images',False),repair_scope_id=rid)

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

@app.get('/api/projects/{wid}/artifacts')
def project_artifacts(wid:str,kind:str|None=None):return project_files.list_artifacts(wid,kind)

@app.get('/api/projects/{wid}/artifacts/{artifact_id}/download')
def project_artifact_download(wid:str,artifact_id:str):
    path=project_files.artifact_path(wid,artifact_id)
    with Session() as s:item=require(s,ProjectArtifact,artifact_id)
    return FileResponse(path,filename=item.name)

@app.get('/api/projects/{wid}/files')
def project_files_index(wid:str):
    project=project_files.public_project(wid)
    artifacts=project_files.list_artifacts(wid)
    folders=[{'kind':kind,'path':path,'count':sum(1 for item in artifacts if item['kind']==kind)}
             for kind,path in project_files.FOLDERS.items()]
    return {'project':project,'folders':folders,'artifacts':artifacts}

@app.get('/api/projects/{wid}/library')
def project_library_index(wid:str):return project_library.list_library(wid)

@app.get('/api/projects/{wid}/library/{artifact_id}/workbook')
def project_library_workbook(wid:str,artifact_id:str,sheet:str='',offset:int=0,
                             limit:int=50,query:str=''):
    return project_library.workbook_page(wid,artifact_id,sheet=sheet,offset=offset,
                                         limit=limit,query=query)

@app.get('/api/projects/{wid}/library/{artifact_id}/images/{image_id}')
def project_library_image(wid:str,artifact_id:str,image_id:str):
    return project_library.image_thumbnail(wid,artifact_id,image_id)

@app.get('/api/projects/{wid}/library/{artifact_id}/html')
def project_library_html(wid:str,artifact_id:str):
    return PlainTextResponse(project_library.html_text(wid,artifact_id),
                             media_type='text/plain; charset=utf-8',
                             headers={'X-Content-Type-Options':'nosniff'})

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
    task=text_facts.prepare(dataset['id']) if external_effects.effects_allowed('model_api') else None
    if task and task['status']=='queued':batch.launch(task['id'])
    return {**dataset,'text_job_id':task['id'] if task else None,'profile':worksheet_query.profile(dataset['id'])}

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

@app.get('/api/datasets/{did}/conversations')
def conversations_list(did:str):
    from .chat_threads import listing
    return listing(did)

@app.post('/api/datasets/{did}/conversations')
def conversations_create(did:str,body:dict):
    from .chat_threads import create
    return create(did,body.get('name','新对话'))

@app.post('/api/datasets/{did}/conversations/{cid}')
def conversations_update(did:str,cid:str,body:dict):
    from .chat_threads import update
    return update(did,cid,body)

@app.delete('/api/datasets/{did}/conversations/{cid}')
def conversations_delete(did:str,cid:str):
    from .chat_threads import destroy
    return destroy(did,cid)

@app.post('/api/datasets/{did}/conversations/{cid}/attachments')
async def conversation_attachments(did:str,cid:str,files:list[UploadFile]=File(...)):
    from .chat_images import upload,MAX_FILE
    payload=[]
    for f in files:
        data=await f.read(MAX_FILE+1);payload.append((f.filename,data))
    return upload(did,cid,payload)

@app.get('/api/datasets/{did}/conversations/{cid}/attachments/{aid}')
def conversation_attachment(did:str,cid:str,aid:str,thumbnail:bool=False):
    from .chat_images import require_attachment
    with Session() as s:
        item=require_attachment(s,did,cid,aid);path=item.data['thumb_path' if thumbnail else 'path'];mime='image/jpeg' if thumbnail else item.data['mime']
    return FileResponse(path,media_type=mime)

@app.post('/api/datasets/{did}/conversations/{cid}/attachments/{aid}/bind')
def conversation_attachment_bind(did:str,cid:str,aid:str,body:dict):
    from .chat_images import bind
    return bind(did,cid,aid,body.get('product_id',''))

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
    # Confirming a local schema/field change is safe offline. Proposal changes marked as
    # ``execute`` create model-backed batch work and must pass the same explicit policy gate.
    with Session() as s:
        proposal=require(s,Proposal,pid)
        if proposal.dataset_id!=did:raise ValueError('提案不属于当前数据集')
        if any(isinstance(change,dict) and change.get('kind')=='execute' for change in (proposal.changes or [])):
            external_effects.assert_allowed('model_api')
    result=draft.confirm(did,pid,body.get('product_ids'),body.get('sample',False),body.get('text_model'),body.get('vision_model'),body.get('confirmation_key'),body.get('text_credential_id'),body.get('vision_credential_id'))
    for j in result:batch.launch(j['id'])
    return result

@app.post('/api/drafts/{did}/proposals/{pid}/reject')
def draft_reject(did:str,pid:str):
    with draft.WRITE_LOCK,Session.begin() as s:
        p=require(s,Proposal,pid)
        if p.dataset_id!=did or p.status!='pending':raise ValueError('提案不可撤回')
        p.status='rejected';draft.emit(s,did,'proposal',obj(p));draft.emit(s,did,'proposal_group',obj(p));return obj(p)

@app.patch('/api/drafts/{did}/cells/{product_id}/{column_id}')
def draft_correct(did:str,product_id:str,column_id:str,body:dict):return draft.correct(did,product_id,column_id,body.get('value'),body.get('expected_edit'))

@app.post('/api/drafts/{did}/versions')
def draft_version(did:str):
    item=draft.save_version(did)
    data=draft.sheet(did,item['id'])
    workspace_id=(data.get('dataset') or {}).get('workspace_id')
    if workspace_id:
        export_path=draft_export.export(did,[column['id'] for column in data.get('columns',[])],
                                        item['id'],unknown_as_blank=True)
        item['project_artifact']=project_files.register_file(workspace_id,'data_x',export_path,
            source_type='data_x_snapshot',source_id=item['id'],name=f"Data-X-V{item['version']}.xlsx",
            metadata={'dataset_id':did,'version':item['version']})
    return item

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

@app.get('/api/agent/sessions/{session_id}/events')
async def agent_events(session_id:str,request:Request,after_sequence:str|None=None):
    """Agent event stream, independent of the draft stream: sequence is the cursor, so a client
    resumes with after_sequence or Last-Event-ID."""
    from fastapi.responses import StreamingResponse
    from .agent import events as agent_store,stream as agent_stream
    if agent_store.get_session(session_id) is None:raise HTTPException(status_code=404,detail='Agent 会话不存在')
    cursor=agent_stream.resolve_cursor(after_sequence,request.headers.get('last-event-id'))
    return StreamingResponse(agent_stream.event_stream(session_id,cursor,disconnected=request.is_disconnected),
        media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

app.include_router(agent_router)
app.include_router(keyword_research_routes.router)
app.include_router(xunlongjue_router)

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
