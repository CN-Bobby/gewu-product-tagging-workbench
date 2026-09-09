"""Browser layout evidence for the task panel on the isolated beta service."""
import argparse,hashlib,json
from pathlib import Path

from playwright.sync_api import sync_playwright,expect

parser=argparse.ArgumentParser()
parser.add_argument('--base',default='http://127.0.0.1:8877')
parser.add_argument('--out',type=Path,required=True)
parser.add_argument('--width',type=int)
parser.add_argument('--height',type=int)
parser.add_argument('--zoom',type=float)
args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
cases=[];page_errors=[]
matrix=[(args.width,args.height,args.zoom)] if args.width and args.height and args.zoom else [
    (width,height,zoom) for width,height in ((1366,768),(1920,1080)) for zoom in (1,1.25,1.5)]

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True)
    for width,height,zoom in matrix:
            context=browser.new_context(viewport={'width':width,'height':height})
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(args.base);expect(page.locator('.product-table tbody tr')).to_have_count(352,timeout=15000)
            toggle=page.locator('.jobs-title button')
            if toggle.inner_text()=='展开任务':toggle.click()
            expect(page.locator('.jobs-scroll .job')).not_to_have_count(0)
            page.evaluate('(zoom)=>document.documentElement.style.zoom=zoom',zoom)
            last=page.locator('.jobs-scroll .job').last;last.scroll_into_view_if_needed()
            page.wait_for_timeout(80)
            measurement=last.evaluate("""job=>{
                const scroll=job.closest('.jobs-scroll');
                const sr=scroll.getBoundingClientRect();
                const buttons=[...job.querySelectorAll('.job-actions button')].map(button=>{
                    const r=button.getBoundingClientRect();
                    const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                    return {text:button.textContent.trim(),height:r.height,left:r.left,right:r.right,top:r.top,bottom:r.bottom,
                            visible:r.left>=0&&r.right<=innerWidth&&r.top>=sr.top&&r.bottom<=Math.min(sr.bottom,innerHeight),
                            clickable:hit===button||button.contains(hit)};
                });
                return {scroll:{top:sr.top,bottom:sr.bottom,height:sr.height,scrollHeight:scroll.scrollHeight},buttons};
            }""")
            assert measurement['buttons'] and all(item['visible'] and item['clickable'] and item['height']>=32*zoom-.5 for item in measurement['buttons']),measurement
            detail=last.get_by_role('button',name='详情',exact=True);detail.click();expect(page.locator('.modal')).to_be_visible()
            page.locator('.modal .panel-heading button').evaluate('button=>button.click()');expect(page.locator('.modal')).to_have_count(0)
            # Minimum resized height must keep the action row reachable.
            page.locator('.jobs').evaluate("node=>node.style.height='1px'")
            jobs_height=page.locator('.jobs').evaluate('node=>node.getBoundingClientRect().height')
            assert jobs_height>=132*zoom-.5,jobs_height
            last.scroll_into_view_if_needed()
            assert last.get_by_role('button',name='详情',exact=True).is_visible()
            expanded=args.out/f'task-panel-expanded-{width}x{height}-{int(zoom*100)}.png'
            page.screenshot(path=str(expanded),full_page=False)
            toggle.click();collapsed=page.locator('.jobs').evaluate('node=>node.getBoundingClientRect().height')
            assert abs(collapsed-43*zoom)<2,(collapsed,zoom)
            page.get_by_role('button',name='表格专注',exact=True).click()
            assert page.locator('.workbench').get_attribute('class')=='workbench table-focus'
            screenshot=args.out/f'table-focus-{width}x{height}-{int(zoom*100)}.png'
            page.screenshot(path=str(screenshot),full_page=False)
            cases.append({'viewport':[width,height],'zoom_percent':int(zoom*100),'buttons':measurement['buttons'],
                          'expanded_min_height':jobs_height,'collapsed_height':collapsed,
                          'expanded_screenshot':str(expanded),'expanded_screenshot_sha256':hashlib.sha256(expanded.read_bytes()).hexdigest(),
                          'focus_screenshot':str(screenshot),'focus_screenshot_sha256':hashlib.sha256(screenshot.read_bytes()).hexdigest()})
            page_errors.extend(errors);context.close()
    browser.close()

assert not page_errors,page_errors
report={'base':args.base,'cases':cases,'page_errors':page_errors,'model_requests':0}
suffix='' if len(matrix)>1 else f'-{matrix[0][0]}x{matrix[0][1]}-{int(matrix[0][2]*100)}'
path=args.out/f'report{suffix}.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(path),'cases':len(cases),'all_buttons_visible_clickable':True},ensure_ascii=False))
