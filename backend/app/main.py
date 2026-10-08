import asyncio
import io
import json
import warnings
import os
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Depends, Header, Body, BackgroundTasks, UploadFile, File, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image, UnidentifiedImageError
from . import db, service
from .config import settings, ROOT
from .release import APP_ID, API_CONTRACT_VERSION, source_snapshot, verify_frontend
from .runtime_config import model_runtime_snapshot

# Capture the loaded release once, not the source files from a later edit.
STARTUP_SOURCE_SHA = source_snapshot()['sha256']
FRONTEND_DIR = settings.frontend_dir.resolve()
FRONTEND_BUILD_ID = None
if os.getenv('APP_FRONTEND_DIST'):
    FRONTEND_BUILD_ID = verify_frontend(FRONTEND_DIR, STARTUP_SOURCE_SHA)['build_id']


@asynccontextmanager
async def lifespan(app):
    settings.upload_dir.mkdir(parents=True,exist_ok=True)
    db.init_db()
    with db.connect() as connection:
        app.state.schema_version = connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
    from . import assessment_jobs
    assessment_jobs.init_jobs()
    yield


app=FastAPI(title='日常好物 · 客服与售后',version='1.0.0',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origin_regex=r'https?://(localhost|127\.0\.0\.1)(:\d+)?',allow_methods=['*'],allow_headers=['*'])


def user(authorization: str=Header(default='')):
    if not authorization.startswith('Bearer '):service.fail(401,'请先选择身份进入店铺')
    return service.authenticate(authorization[7:])


async def run_model(tid):
    from .agent import run_turn
    try:
        await run_turn(tid)
    except Exception:
        # No upstream request or secret is ever echoed in a client error.
        service.agent_fail(tid,'这次回复暂时未能完成，消息已保留。请重试或联系人工。')


@app.get('/api/health')
def health():
    ledger=os.getenv('MODEL_BUDGET_LEDGER','')
    budget=os.getenv('MODEL_BUDGET_CNY')
    return {'ok':True,'app_id':APP_ID,'source_sha256':STARTUP_SOURCE_SHA,
            'frontend_build_id':FRONTEND_BUILD_ID,'api_contract_version':API_CONTRACT_VERSION,
            'schema_version':getattr(app.state,'schema_version',None),
            'instance_id':os.getenv('APP_INSTANCE_ID'),
            'model':settings.deepseek_model,'model_configured':bool(settings.deepseek_api_key),
            'model_runtime':model_runtime_snapshot(),
            'budget_enabled':bool(budget and ledger),'budget_limit_cny':float(budget) if budget else None,
            'budget_ledger_fingerprint':hashlib.sha256(str(Path(ledger).resolve()).encode()).hexdigest() if ledger else None,
            'evaluation_db_fingerprint':hashlib.sha256(str(settings.db_path.resolve()).encode()).hexdigest()}
@app.get('/api/personas')
def personas():return service.personas()
@app.post('/api/customers', status_code=201)
def create_customer(data:dict=Body(...)):return service.create_customer(data)
@app.post('/api/sessions')
def sessions(data:dict=Body(...)):return service.login(data.get('persona_id'))
@app.get('/api/me')
def me(u=Depends(user)):return u
@app.get('/api/products')
def products(search:str='',category:str='',u=Depends(user)):return service.products(search,category)
@app.get('/api/products/{pid}')
def product(pid:str,u=Depends(user)):return service.product(pid)
@app.get('/api/inventory/{sid}')
def inventory(sid:str,u=Depends(user)):return service.inventory(sid)
@app.post('/api/quotes')
def quote(data:dict=Body(...),u=Depends(user)):return service.quote(u,data)
@app.post('/api/orders')
def create_order(data:dict=Body(...),u=Depends(user)):return service.create_order(u,data)
@app.get('/api/orders')
def orders(u=Depends(user)):return service.orders(u)
@app.get('/api/orders/{oid}')
def order(oid:str,u=Depends(user)):return service.order(u,oid)
@app.get('/api/orders/{oid}/parts')
def order_parts(oid:str,u=Depends(user)):return service.get_parts(u,oid)
@app.get('/api/policy')
def policy(u=Depends(user)):return service.policy()
@app.get('/api/promotions')
def promotions(u=Depends(user)):return service.promotions()
@app.post('/api/conversations')
def create_conversation(data:dict=Body(default={}),u=Depends(user)):return service.create_conversation(u,bool(data.get('fresh',False)))
@app.get('/api/conversations')
def conversations(u=Depends(user)):return service.conversations(u)
@app.get('/api/conversations/{cid}')
def conversation(cid:str,u=Depends(user)):return service.conversation_detail(u,cid)
@app.patch('/api/conversations/{cid}/context')
def context(cid:str,data:dict=Body(...),u=Depends(user)):return service.set_context(u,cid,data)
@app.patch('/api/conversations/{cid}/preferences')
def preferences(cid:str,data:dict=Body(...),u=Depends(user)):
    from . import conversation_state
    return conversation_state.patch(u,cid,data)
@app.post('/api/conversations/{cid}/messages')
def message(cid:str,background:BackgroundTasks,data:dict=Body(...),u=Depends(user)):
    out=service.send_message(u,cid,data)
    if out.pop('_new',False):background.add_task(run_model,out['turn_id'])
    return out
@app.get('/api/turns/{tid}')
def turn(tid:str,u=Depends(user)):return service.turn(u,tid)
@app.get('/api/turns/{tid}/events')
async def events(tid:str,request:Request,u=Depends(user)):
    initial=service.turn(u,tid)
    async def stream():
        yield 'data: '+json.dumps({'type':'status','phase':initial['phase']},ensure_ascii=False)+'\n\n'
        index=0
        while True:
            if await request.is_disconnected():break
            updates,index=service.events_since(tid,index)
            for event in updates:yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
            current=service.turn(u,tid)
            if current['status']!='pending':
                event={'type':'error','error':current['error']} if current['status']=='failed' else {'type':'done','answer':current['answer'],'status':current['status']}
                yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                break
            await asyncio.sleep(.25)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
@app.post('/api/conversations/{cid}/retry')
def retry(cid:str,background:BackgroundTasks,data:dict=Body(...),u=Depends(user)):
    out=service.retry_turn(u,cid,data)
    if out.pop('_new',False):background.add_task(run_model,out['turn_id'])
    return out
@app.post('/api/conversations/{cid}/handoff')
def handoff(cid:str,u=Depends(user)):return service.handoff(u,cid)
@app.post('/api/conversations/{cid}/resume')
def resume(cid:str,background:BackgroundTasks,data:dict=Body(default={}),u=Depends(user)):
    out=service.resume(u,cid,data)
    if out.pop('_new',False):background.add_task(run_model,out['turn_id'])
    return out
@app.post('/api/conversations/{cid}/staff-messages')
def staff_message(cid:str,data:dict=Body(...),u=Depends(user)):return service.staff_message(u,cid,data)


@app.post('/api/attachments')
async def upload(file:UploadFile=File(...),u=Depends(user)):
    service.require_customer(u)
    content=await file.read(10*1024*1024+1)
    if len(content)>10*1024*1024:service.fail(413,'每张图片不能超过 10 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            im=Image.open(io.BytesIO(content));fmt=im.format
            if fmt not in ('JPEG','PNG','WEBP'):service.fail(422,'只支持 JPEG、PNG、WebP 图片')
            if im.width*im.height>20_000_000:service.fail(413,'图片分辨率过大，请使用小于两千万像素的图片')
            im.verify()
            im=Image.open(io.BytesIO(content));im.load()
    except HTTPException:raise
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):service.fail(422,'图片无法读取，请换一张清晰的图片')
    aid=db.uid('image');extension={'JPEG':'jpg','PNG':'png','WEBP':'webp'}[fmt];mime={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}[fmt]
    path=settings.upload_dir/(aid+'.'+extension);settings.upload_dir.mkdir(parents=True,exist_ok=True)
    # Re-encode to remove metadata and any non-image trailing content.
    if fmt=='JPEG':im=im.convert('RGB')
    im.save(path,format=fmt)
    with db.transaction() as c:c.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',(aid,u['id'],Path(file.filename or '图片').name,mime,str(path.resolve()),db.now()))
    return {'id':aid,'name':Path(file.filename or '图片').name,'mime_type':mime}


@app.get('/api/attachments/{aid}')
def attachment(aid:str,u=Depends(user)):
    with db.connect() as c:
        row=service.one(c,'SELECT * FROM attachments WHERE id=?',(aid,),'图片不存在');service.owner(u,row['user_id'])
        path=Path(row['path']).resolve()
        if not path.is_relative_to(settings.upload_dir.resolve()) or not path.exists():service.fail(404,'图片不存在')
        return FileResponse(path,media_type=row['mime_type'],headers={'Cache-Control':'private, max-age=3600'})


@app.post('/api/assessments')
def create_assessment(background:BackgroundTasks,data:dict=Body(...),u=Depends(user)):
    from . import assessment_jobs
    out=assessment_jobs.create(u,data)
    if out.pop('_new',False):background.add_task(assessment_jobs.run,out['id'])
    return out
@app.get('/api/assessments/{aid}')
def get_assessment(aid:str,u=Depends(user)):
    from . import assessment_jobs
    return assessment_jobs.get(u,aid)


@app.post('/api/cases')
def create_case(data:dict=Body(...),u=Depends(user)):return service.create_case(u,service.public_case_input(data,reset_facts=True))
@app.get('/api/cases')
def cases(u=Depends(user)):return service.get_cases(u)
@app.patch('/api/cases/{cid}')
def patch_case(cid:str,data:dict=Body(...),u=Depends(user)):return service.patch_case(u,cid,service.public_case_input(data))
@app.post('/api/cases/{cid}/submit')
def submit_case(cid:str,data:dict=Body(...),u=Depends(user)):return service.submit_case(u,cid,data)
@app.post('/api/cases/{cid}/supplements')
def supplement_case(cid:str,data:dict=Body(...),u=Depends(user)):return service.supplement_case(u,cid,service.public_case_input(data,reset_facts=True))
@app.post('/api/cases/{cid}/review')
def review_case(cid:str,data:dict=Body(...),u=Depends(user)):return service.review_case(u,cid,data)
@app.post('/api/cases/{cid}/complete')
def complete_case(cid:str,data:dict=Body(...),u=Depends(user)):return service.complete_case(u,cid,data)
@app.get('/api/admin/data')
def admin_data(u=Depends(user)):return service.admin_data(u)
@app.patch('/api/admin/skus/{sid}')
def update_sku(sid:str,data:dict=Body(...),u=Depends(user)):return service.update_sku(u,sid,data)
@app.patch('/api/admin/products/{pid}')
def update_product(pid:str,data:dict=Body(...),u=Depends(user)):return service.update_product(u,pid,data)
@app.put('/api/admin/promotions')
def update_promotions(data:dict=Body(...),u=Depends(user)):return service.update_promotions(u,data)
@app.post('/api/admin/policies')
def publish_policy(data:dict=Body(...),u=Depends(user)):return service.publish_policy(u,data)


from . import support_routes
app.include_router(support_routes.build_router(user))
from . import catalog_routes
app.include_router(catalog_routes.build_router(user))
from . import packing
app.include_router(packing.build_router(user))
from . import work_operations_routes
app.include_router(work_operations_routes.build_router(user))
from . import packing_sources, packing_workflow, packing_exports, packing_demo, logistics_routes, operations_dashboard_routes
for module in (packing_sources, packing_workflow, packing_exports, packing_demo, logistics_routes, operations_dashboard_routes):
    app.include_router(module.build_router(user))


@app.get('/{path:path}')
def frontend(path:str):
    if path.startswith('api/') or path=='api':service.fail(404,'接口不存在')
    dist=FRONTEND_DIR;target=(dist/path).resolve()
    if target.is_relative_to(dist) and target.is_file():return FileResponse(target)
    if path.startswith('assets/'):service.fail(404,'资源不存在')
    if (dist/'index.html').exists():return FileResponse(dist/'index.html')
    service.fail(503,'前端尚未构建，请运行启动脚本或 npm run build')
