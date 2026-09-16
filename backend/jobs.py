import json
import threading
from sqlalchemy import select, func
from .db import *
from .harness import VISION_MODEL, executable, runtime_dir
from .agent.reasonix import ExecutionBudget

active={}
chats={}
lock=threading.Lock()
# Background budgets. A worker may legitimately spend minutes on images and tools and has nobody to
# ask, so it is allowed to be quiet longer than the interactive surface; the coordinator only reads
# and proposes. The wall clock is the same bound for all three.
WORKER_EXECUTION_BUDGET=ExecutionBudget(first_activity_timeout=180,idle_timeout=300,
                                        tool_wait_timeout=360,turn_wall_clock_timeout=1200)
COORDINATOR_EXECUTION_BUDGET=ExecutionBudget(first_activity_timeout=120,idle_timeout=240,
                                             tool_wait_timeout=360,turn_wall_clock_timeout=1200)
WORKER_INSTRUCTION=('执行单商品打标。先 get_products 读取本商品，再用 view_product_image 检查图片，'
                    '最后 record_product_tags 保存完整结果。不要只在回复里给结果。严格采用已确认的标签定义：\n')

def _refuse_interaction(holder,event):
    """Refuse an interaction nobody is watching, so the peer is never left waiting on it.

    A background runtime has no conversation to resume and therefore owns no business question;
    the capability is not registered for it in the first place. This is the second layer: an
    interaction asked for anyway is settled through the backend's own host API rather than dropped.
    """
    kind=(event or {}).get('type')
    if kind not in ('question_requested','permission_requested'):return
    method=holder[0].cancel_question if kind=='question_requested' else holder[0].cancel_permission
    try:method(event.get('interaction_id'))
    except Exception:pass      # already settled, or the runtime is gone: nothing left to answer

def has_active_reasonix_work():
    """Whether this process owns a live worker run or coordinator turn. The settings lock asks this.

    Read-only: it only reports the registry this module already keeps, so asking cannot start,
    resume or stop anything.
    """
    with lock:return bool(active or chats)


def _backend(dataset_id,model,suffix,product_id=None,role='coordinator',sink=None,budget=None,credentials=None):
    """Build the one backend this job owns, described only in backend-neutral launch facts.

    The job — never the peer — carries the trusted execution context: it chooses the runtime
    directory and the isolated provider environment, so dataset, run and product identity reach
    the runtime through configuration instead of being asserted in a prompt. Background work has
    no conversation behind it, so it never owns a business question. `role` selects the harness
    runtime-directory identity (its instructions and MCP surface) and stays out of the backend
    configuration, which knows nothing about coordinator or worker — the caller decides the time
    budget too, and passes it here explicitly. `credentials` is the selection the caller resolved
    once for the whole run, not a lookup performed here.
    """
    from .agent.reasonix import ReasonixAdapter,ReasonixLaunchConfig
    from .model_settings import normalize_reference
    from .runtime_isolation import environment
    cwd=runtime_dir(dataset_id,suffix,product_id,model if product_id else VISION_MODEL,role=role)
    holder=[]
    def emit(event):
        _refuse_interaction(holder,event)
        if sink is not None:sink(event)
    adapter=ReasonixAdapter(ReasonixLaunchConfig(executable=executable(),model=normalize_reference(model),
        cwd=str(cwd),env=environment(dataset_id,None,credentials or {}),
        runtime_key=':'.join(['job',dataset_id,suffix]+([str(product_id)] if product_id else [])),
        budget=budget))
    holder.append(adapter)
    adapter.bind(emit)
    adapter.start()
    return adapter

def recover():
    with Session.begin() as s:
        for r in s.scalars(select(Run).where(Run.status.in_(['running','queued']))):r.status='interrupted'
        for m in s.scalars(select(Message).where(Message.status=='running')):m.status='interrupted'
        for q in s.scalars(select(ChatQuestion).where(ChatQuestion.status=='pending')):
            q.status='interrupted';q.error_code='reasonix_process_error'
        for item in s.scalars(select(WorkbenchMeta).where(WorkbenchMeta.kind=='active_chat_turn')):s.delete(item)

def launch_run(run_id):
    with lock:
        if run_id in active:raise ValueError('任务正在执行')
        active[run_id]={'stop':False,'client':None}
    threading.Thread(target=_execute,args=(run_id,),daemon=True).start()

def _execute(run_id):
    try:
        with Session.begin() as s:
            r=require(s,Run,run_id);r.status='running'
            dataset_id=r.dataset_id;model=r.model;ids=r.product_ids;schema=obj(require(s,Schema,r.schema_id))
            r.errors={}
        # One resolution for the whole run: every product of this run launches with the same
        # credential, so a settings change halfway through cannot move it onto another key.
        from .runtime_isolation import CredentialUnavailable,credential_selection
        try:provider,credential_id=credential_selection(model)
        except CredentialUnavailable as error:
            with Session.begin() as s:
                r=require(s,Run,run_id);r.status='interrupted'
                r.errors={**(r.errors or {}),'run':str(error)[:1200]}
            return
        credentials={provider:credential_id} if credential_id else {}
        for pid in ids:
            if active[run_id]['stop']:break
            with Session() as s:
                if s.scalar(select(Tag).where(Tag.run_id==run_id,Tag.product_id==pid)):continue
            client=None
            try:
                client=_backend(dataset_id,model,run_id,pid,role='worker',budget=WORKER_EXECUTION_BUDGET,
                                credentials=credentials)
                active[run_id]['client']=client
                if active[run_id]['stop']:break
                client.send(WORKER_INSTRUCTION+json.dumps(schema['definitions'],ensure_ascii=False))
                with Session() as s:
                    if not s.scalar(select(Tag).where(Tag.run_id==run_id,Tag.product_id==pid)):raise RuntimeError('Agent 本轮没有保存结构化结果；请查看模型或工具可用性后重试')
            except Exception as e:
                with Session.begin() as s:
                    r=require(s,Run,run_id);r.errors={**r.errors,pid:str(e)[:1200]}
            finally:
                if client:client.stop()
                active[run_id]['client']=None
        with Session.begin() as s:
            r=require(s,Run,run_id)
            count=s.scalar(select(func.count()).select_from(Tag).where(Tag.run_id==run_id))
            r.status='completed' if count==len(ids) else 'paused' if active[run_id]['stop'] else 'partial'
            if r.status=='completed':require(s,Dataset,dataset_id).stage='CALIBRATION' if r.kind=='sample' else 'EXPORT'
            audit(s,dataset_id,'run_finished',{'run_id':run_id,'status':r.status,'success':count,'total':len(ids)})
    finally:
        with lock:active.pop(run_id,None)

def stop_run(run_id):
    with lock:
        item=active.get(run_id)
        if item:
            item['stop']=True
            if item['client']:item['client'].cancel()
    return {'stopping':bool(item)}

def resume_run(run_id):
    with Session.begin() as s:
        r=require(s,Run,run_id)
        if r.status not in ('partial','paused','interrupted'):raise ValueError('当前任务不可重试')
        r.status='queued'
    launch_run(run_id)

def start_chat(dataset_id,text,model):
    if not text.strip() or len(text)>20000:raise ValueError('请输入 1–20000 字的消息')
    with lock:
        if dataset_id in chats:raise ValueError('Reasonix 正在回复，可先停止当前回复')
        chats[dataset_id]={'client':None,'stop':False}
    with Session.begin() as s:
        require(s,Dataset,dataset_id)
        s.add(Message(dataset_id=dataset_id,role='user',text=text))
        response=Message(dataset_id=dataset_id,role='assistant',text='',status='running')
        s.add(response);s.flush();ident=response.id
    threading.Thread(target=_chat,args=(dataset_id,ident,text,model),daemon=True).start()
    return {'message_id':ident}

def _chat(dataset_id,ident,text,model):
    client=None
    def event(e):
        """Project the runtime's events into the record this turn belongs to.

        Only what that record already stores is kept: assistant text becomes the message body, a
        tool lifecycle becomes one stored tool row in the shape the record has always used. Nothing
        from the wire, and nothing about the runtime, is written down.
        """
        kind=(e or {}).get('type')
        if kind=='assistant_text_delta':
            chunk=e.get('text') or ''
            if not chunk:return
            with Session.begin() as s:
                m=require(s,Message,ident);m.text=(m.text or '')+chunk
            return
        if kind in ('tool_call_started','tool_call_settled'):
            status={'completed':'completed','failed':'failed'}.get(e.get('host_status'),'pending')
            stored={'type':'tool','toolCallId':e.get('tool_call_id'),
                    'title':e.get('tool') or e.get('server') or '工作台工具','status':status}
            with Session.begin() as s:
                m=require(s,Message,ident);m.events=(m.events+[stored])[-100:]
    try:
        # Resolved once for this turn, then carried by the backend for its whole life.
        from .runtime_isolation import credential_selection
        provider,credential_id=credential_selection(model)
        credentials={provider:credential_id} if credential_id else {}
        client=_backend(dataset_id,model,'chat',role='coordinator',sink=event,
                        budget=COORDINATOR_EXECUTION_BUDGET,credentials=credentials)
        chats[dataset_id]['client']=client
        if chats[dataset_id]['stop']:raise RuntimeError('回复已停止')
        # Replay bounded visible history; business context lives in MCP/database, not hidden reasoning.
        with Session() as s:
            history=s.scalars(select(Message).where(Message.dataset_id==dataset_id,Message.id!=ident,Message.status=='completed').order_by(Message.created_at.desc()).limit(12)).all()
        context='\n'.join(f'{m.role}: {m.text[:5000]}' for m in reversed(history))
        client.send('以下是可见对话历史，用于理解用户需求；当前事实和状态请通过 MCP 查询。\n'+context+'\n本轮用户要求：'+text)
        with Session.begin() as s:
            m=require(s,Message,ident);m.status='completed'
            if not m.text:m.text='Reasonix 已结束本轮，未返回可见文字。请检查右侧标签与任务状态。'
    except Exception as e:
        with Session.begin() as s:
            m=require(s,Message,ident);m.status='failed';m.text=(m.text or '')+'\n运行失败：'+str(e)[:1200]
    finally:
        if client:client.stop()
        with lock:chats.pop(dataset_id,None)

def stop_chat(dataset_id):
    with lock:
        item=chats.get(dataset_id)
        if item:
            item['stop']=True
            if item['client']:item['client'].cancel()
    return {'stopping':bool(item)}
