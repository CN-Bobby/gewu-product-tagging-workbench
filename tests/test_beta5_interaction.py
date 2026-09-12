from backend.conversation_presentation import build


def test_tool_lifecycle_has_one_current_state_per_call():
    events=[{'type':'tool','toolCallId':'a','title':'读取表格','status':s} for s in ('pending','completed')]
    events.append({'type':'tool','toolCallId':'b','title':'读取表格','status':'failed'})
    result=build('已读取',events=events)
    assert result['activity']==[{'title':'读取表格','status':'completed'}, {'title':'读取表格','status':'failed'}]


def test_unsupported_question_becomes_plain_question(dataset,monkeypatch):
    from backend import conversation
    from backend.db import Session,Message,uid
    from backend.harness import HarnessError,instruction_hash
    from types import SimpleNamespace
    did=dataset['id'];ident=uid()
    class Client:
        closed=False
        proc=SimpleNamespace(poll=lambda:None)
        fallback_question={'prompt':'要观察哪些方面？','options':[{'Label':'颜色'},{'label':'结构'}]}
        def prompt(self,text):raise HarnessError('unsupported_question')
    client=Client();client.instruction_hash=instruction_hash()
    monkeypatch.setitem(conversation.clients,did,client)
    with Session.begin() as s:s.add(Message(id=ident,dataset_id=did,role='assistant',text='',status='running'))
    conversation.run(did,ident,'turn-fallback','看图片','test/model')
    with Session() as s:
        message=s.get(Message,ident)
        assert message.status=='completed' and message.error_code is None
        assert '颜色' in message.text and '结构' in message.text
        assert '直接输入回答' in message.text


def test_managed_question_accepts_multiple_and_text_once(dataset):
    from backend import business_questions as questions
    from backend.db import Session,Message,WorkbenchMeta,ChatQuestion,uid
    from sqlalchemy import select
    from concurrent.futures import ThreadPoolExecutor
    import time
    did=dataset['id'];mid=uid();turn=uid()
    with Session.begin() as s:
        s.add(Message(id=mid,dataset_id=did,role='assistant',text='',status='running'))
        s.add(WorkbenchMeta(id='active-chat:'+did,dataset_id=did,kind='active_chat_turn',data={'turn_id':turn,'message_id':mid}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(questions.ask,did,'选哪些特征？',['颜色','结构'],True)
        qid=None
        for _ in range(100):
            with Session() as s:
                q=s.scalar(select(ChatQuestion).where(ChatQuestion.turn_id==turn))
                if q:qid=q.id;break
            time.sleep(.02)
        assert qid
        first=questions.answer(did,qid,['2','1'],'只看主体')
        second=questions.answer(did,qid,['1','2'],'只看主体')
        assert first['id']==second['id'] and first['status']=='answered'
        result=future.result(timeout=3)
        assert result['answer']['labels']==['颜色','结构'] and result['approval'] is False


def test_conversation_images_have_stable_numbers_and_bind_explicitly(dataset):
    from backend import chat_threads,chat_images,draft
    from backend.db import Session,Asset
    from PIL import Image
    import io
    did=dataset['id'];thread=chat_threads.listing(did)[0];row=draft.sheet(did)['rows'][0]
    first=open(row['assets'][0]['path'],'rb').read()
    buf=io.BytesIO();Image.new('RGB',(12,12),'red').save(buf,'PNG');second=buf.getvalue()
    uploaded=chat_images.upload(did,thread['id'],[('one.png',first),('duplicate.png',first),('two.png',second)])
    assert [x['number'] for x in uploaded]==[1,1,2]
    bound=chat_images.bind(did,thread['id'],uploaded[0]['id'],row['id'])
    assert bound['bound_product_id']==row['id']
    with Session() as s:
        asset=s.get(Asset,bound['asset_id']);assert asset.image_type=='supplemental' and asset.product_id==row['id']


def test_conversation_recycle_restore_and_permanent_delete(dataset):
    from backend import chat_threads,chat_images,draft
    from backend.db import Session,WorkbenchMeta
    from PIL import Image
    import io
    did=dataset['id'];item=chat_threads.create(did,'可删除对话')
    one=io.BytesIO();Image.new('RGB',(8,8),'red').save(one,'PNG')
    two=io.BytesIO();Image.new('RGB',(8,8),'blue').save(two,'PNG')
    attachments=chat_images.upload(did,item['id'],[('one.png',one.getvalue()),('two.png',two.getvalue())])
    chat_images.bind(did,item['id'],attachments[0]['id'],draft.sheet(did)['rows'][0]['id'])
    assert chat_threads.update(did,item['id'],{'deleted':True})['deleted']
    assert not chat_threads.update(did,item['id'],{'deleted':False})['deleted']
    chat_threads.update(did,item['id'],{'deleted':True});assert chat_threads.destroy(did,item['id'])['deleted']
    assert all(x['id']!=item['id'] for x in chat_threads.listing(did))
    with Session() as s:
        assert s.get(WorkbenchMeta,attachments[0]['id']) is not None
        assert s.get(WorkbenchMeta,attachments[1]['id']) is None


def test_adopted_source_column_protects_nonblank_and_exports_in_place(dataset):
    from backend import source_labels,draft,draft_export
    from backend.db import Session,Product
    import openpyxl
    did=dataset['id']
    with Session.begin() as s:
        rows=list(s.query(Product).filter(Product.dataset_id==did).order_by(Product.row))
        values=[None,' ',0,False,'无','未知','红|蓝','红','越界','蓝']
        for row,value in zip(rows,values):row.raw={**row.raw,'人工标签':value}
        ds=s.get(__import__('backend.db',fromlist=['Dataset']).Dataset,did);ds.headers=[*ds.headers,'人工标签']
    col=source_labels.adopt(did,{'header':'人工标签','name':'颜色标签','definition':'按人工记录的颜色继续补充空白','options':['红','蓝'],'separator':'|'})
    sheet=draft.sheet(did);cells=[r['cells'][col['id']] for r in sheet['rows']]
    assert sum(c['manual'] is not None for c in cells)==8
    assert cells[0]['status']=='pending' and cells[1]['status']=='pending'
    assert cells[2]['value']==0 and cells[3]['value'] is False
    assert cells[8]['status']=='review'
    wb=openpyxl.load_workbook(draft_export.export(did,[col['id']]),data_only=True);ws=wb['Product_Result']
    headers=[x.value for x in ws[1]];assert headers.count('人工标签')==1 and '颜色标签' not in headers
    assert ws.cell(4,headers.index('人工标签')+1).value==0
    before=col['revision'];disabled=source_labels.set_active(did,col['id'],False)
    assert disabled['revision']==before
    wb=openpyxl.load_workbook(draft_export.export(did,[col['id']]),data_only=True);ws=wb['Product_Result']
    headers=[x.value for x in ws[1]];assert headers.count('人工标签')==1 and '颜色标签' not in headers
    assert ws.cell(4,headers.index('人工标签')+1).value==0
