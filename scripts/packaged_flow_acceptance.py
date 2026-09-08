"""Real browser + model flow against bundled runtime only; test data, never production."""
import json,os,time,tomllib,uuid
from pathlib import Path
import httpx
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1];BASE=os.environ.get('ACCEPTANCE_URL','http://127.0.0.1:8901')
health=httpx.get(BASE+'/api/health').json()
assert 'release-verification' in health['storage'] and health['version'].startswith('0.5.')
out=ROOT/'storage/release-verification'/('packaged-flow-'+uuid.uuid4().hex);out.mkdir()
native=Path(os.environ['APPDATA'])/'reasonix'
config=tomllib.loads((native/'config.toml').read_text(encoding='utf-8-sig'))
provider=next(p for p in config['providers'] if p['name']=='deepseek')
secret=dotenv_values(native/'.env')[provider['api_key_env']]
response=httpx.post(BASE+'/api/settings/models',json={'name':'deepseek','base_url':provider['base_url'],
    'models':['deepseek-v4-flash','deepseek-v4-flash-vision-exp'],'api_key':secret},timeout=20)
response.raise_for_status();del secret
checks=[]
def wait_jobs(page,did,minimum=1):
    end=time.monotonic()+120
    while time.monotonic()<end:
        state=page.request.get(BASE+'/api/drafts/'+did).json()
        if len(state['jobs'])>=minimum and not any(j['status'] in ('running','queued','pausing','cancelling') for j in state['jobs']):
            assert all(j['status']=='completed' for j in state['jobs']),[(j['id'],j['status']) for j in state['jobs']]
            return state
        page.wait_for_timeout(300)
    raise RuntimeError('Packaged task timed out')
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True)
    page=browser.new_page(viewport={'width':1500,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE)
    previous=page.get_by_label('数据集',exact=True).input_value()
    page.locator('input[type=file]').set_input_files(str(ROOT/'测试10行.xlsx'))
    expect(page.get_by_text('核对表头与项目',exact=True)).to_be_visible()
    expect(page.get_by_label('映射SKU',exact=True)).not_to_have_value('')
    page.get_by_role('button',name='导入独立工作表',exact=True).click()
    expect(page.get_by_label('数据集',exact=True)).not_to_have_value(previous,timeout=30000)
    expect(page.locator('.product-table tbody tr')).to_have_count(10,timeout=30000)
    first=page.get_by_label('数据集',exact=True).input_value();state=wait_jobs(page,first)
    checks.append({'first_import':first,'text_metrics':state['jobs'][0]['metrics']})
    page.get_by_role('button',name='新增标签',exact=True).click()
    page.get_by_label('中文标签名称',exact=True).fill('套装件数')
    page.get_by_label('判断定义与边界',exact=True).fill('联合读取SKU、详细参数和商品标题，明确的2 PCS、Two Piece对应2件套，3 PCS对应3件套。尺码年龄不是件数，冲突或未明确时未知。')
    page.get_by_role('button',name='生成待确认提案',exact=True).click()
    page.get_by_role('button',name='确认直接执行',exact=True).click()
    state=wait_jobs(page,first,2);column=state['columns'][0]['id']
    assert state['jobs'][-1]['metrics']['vision_requests']==0
    first_tag=page.locator('.product-table tbody tr').first.locator('td.tag-cell').first
    first_tag.dblclick();editor=page.get_by_label('修改单元格',exact=True)
    editor.fill('人工验收值');editor.press('Enter')
    expect(first_tag).to_contain_text('人工验收值')
    page.get_by_role('button',name='标签标准',exact=True).click()
    page.get_by_label('模板名称',exact=True).fill('验收件数标准')
    page.get_by_role('button',name='保存标准修订',exact=True).click()
    expect(page.get_by_text('已保存修订1',exact=True)).to_be_visible()
    page.locator('.modal').get_by_role('button',name='关闭',exact=True).click()
    page.get_by_role('button',name='保存版本',exact=True).click()
    page.locator('input[type=file]').set_input_files({'name':'二月验收.xlsx','mimeType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','buffer':(ROOT/'测试10行.xlsx').read_bytes()})
    expect(page.get_by_text('核对表头与项目',exact=True)).to_be_visible()
    page.get_by_label('上传所属项目',exact=True).select_option(value=state['dataset']['workspace_id'])
    page.get_by_role('button',name='导入独立工作表',exact=True).click()
    expect(page.get_by_label('数据集',exact=True)).not_to_have_value(first,timeout=30000)
    second=page.get_by_label('数据集',exact=True).input_value();state2=wait_jobs(page,second)
    assert not state2['columns']
    page.get_by_role('button',name='标签标准',exact=True).click()
    choice=page.get_by_label('标签模板修订',exact=True)
    expect(choice.locator('option')).to_have_count(2)
    choice.select_option(index=1)
    page.get_by_role('button',name='生成复制提案',exact=True).click()
    page.get_by_role('button',name='确认直接执行',exact=True).click()
    state2=wait_jobs(page,second,2)
    labeljob=next(j for j in state2['jobs'] if not j['spec'].get('kind'))
    assert labeljob['metrics'].get('result_reuse_hits')==10,labeljob['metrics']
    assert labeljob['metrics'].get('requests')==0
    expect(page.locator('.product-table tbody tr').first).to_contain_text('复用人工')
    checks.append({'second_import':second,'reused_job_metrics':labeljob['metrics'],'manual_provenance_visible':True})
    page.screenshot(path=str(out/'reused-month.png'),full_page=False)
    assert not errors,errors
    (out/'report.json').write_text(json.dumps({'health':health,'checks':checks,'browser_errors':errors,
        'boundary':'Bundled runtime direct Python entrypoint; native launcher and clean Windows not verified.'},ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
print(json.dumps({'report':str(out/'report.json'),'checks':checks},ensure_ascii=True))
