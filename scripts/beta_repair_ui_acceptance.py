"""Exercise the cross-job exact-repair proposal in an isolated browser session."""
import argparse,hashlib,json,re
from pathlib import Path

from playwright.sync_api import sync_playwright,expect

parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://127.0.0.1:8877');parser.add_argument('--out',type=Path,required=True)
args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True);errors=[]
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':960});page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto(args.base);expect(page.locator('.product-table tbody tr')).to_have_count(352,timeout=15000)
    page.get_by_role('button',name='任务与局部修复',exact=True).click()
    aggregate=page.locator('button.repair-all');expect(aggregate).to_be_visible();source_jobs=int(re.search(r'\d+',aggregate.inner_text()).group())
    aggregate.click();modal=page.locator('.tools-modal');expect(modal).to_be_visible(timeout=15000)
    expect(modal.locator('input[aria-label^="修复 "]')).not_to_have_count(0,timeout=15000)
    repair_rows=modal.locator('.tools-table tbody tr');total=repair_rows.count()
    enabled=modal.locator('input[aria-label^="修复 "]:not(:disabled)');enabled_count=enabled.count()
    disabled_count=modal.locator('input[aria-label^="修复 "]:disabled').count()
    modal.get_by_role('button',name='清空选择',exact=True).click()
    chosen=[];columns=set()
    for index in range(total):
        row=repair_rows.nth(index);box=row.locator('input[aria-label^="修复 "]')
        if box.is_disabled():continue
        label=row.locator('td').nth(1).locator('small').inner_text().split(' · ')[0]
        if label in columns:continue
        box.check();columns.add(label);chosen.append(box.get_attribute('aria-label'))
        if len(chosen)==2:break
    assert len(chosen)==2 and len(columns)==2,{'chosen':chosen,'columns':sorted(columns),'rows':total,'enabled':enabled_count,'disabled':disabled_count,'html':modal.locator('.tools-table').inner_text()[:2000]}
    modal.get_by_role('button',name='生成 2 个单元格的修复提案',exact=True).click()
    expect(modal).to_have_count(0,timeout=15000)
    proposal=page.locator('.proposal').filter(has=page.get_by_role('heading',name='局部修复：2 个单元格',exact=True))
    expect(proposal).to_be_visible(timeout=15000)
    impact=proposal.locator('.impact-note');expect(impact).to_have_count(2)
    texts=impact.all_inner_texts();assert all('实际重跑 1' in text and '预计请求' in text for text in texts),texts
    screenshot=args.out/'exact-repair-proposal.png';page.screenshot(path=str(screenshot),full_page=False)
    proposal.get_by_role('button',name='撤回',exact=True).click();expect(proposal).to_have_count(0)
    # The deterministic size preset changes the execution contract, so the server must expand to the full column and estimate zero model calls.
    aggregate.click();modal=page.locator('.tools-modal');expect(modal).to_be_visible(timeout=15000)
    expect(modal.locator('input[aria-label^="修复 "]')).not_to_have_count(0,timeout=15000)
    modal.get_by_role('button',name='清空选择',exact=True).click()
    rows=modal.locator('.tools-table tbody tr');selected_label=None
    for index in range(rows.count()):
        row=rows.nth(index);box=row.locator('input[aria-label^="修复 "]')
        if box.is_disabled():continue
        label=row.locator('td').nth(1).locator('small').inner_text().split(' · ')[0]
        if label=='热卖SKU尺码':box.check();selected_label=box.get_attribute('aria-label');break
    assert selected_label
    rule=modal.locator('details').filter(has_text='热卖SKU尺码').last;rule.locator('summary').click()
    rule.get_by_role('button',name='尺码数字提取（零模型请求）',exact=True).click()
    modal.get_by_role('button',name='生成 1 个单元格的修复提案',exact=True).click();expect(modal).to_have_count(0,timeout=15000)
    deterministic=page.locator('.proposal').filter(has=page.get_by_role('heading',name='局部修复：1 个单元格',exact=True));expect(deterministic).to_be_visible(timeout=15000)
    deterministic_text=deterministic.inner_text();assert '实际重跑 352' in deterministic_text and '预计请求 ≤ 0' in deterministic_text,deterministic_text
    deterministic_shot=args.out/'deterministic-size-scope.png';page.screenshot(path=str(deterministic_shot),full_page=False)
    deterministic.get_by_role('button',name='撤回',exact=True).click();expect(deterministic).to_have_count(0)
    browser.close()
assert not errors,errors
report={'base':args.base,'source_jobs':source_jobs,'repair_rows':total,'enabled_text_cells':enabled_count,
        'disabled_visual_cells':disabled_count,'chosen':chosen,'columns':sorted(columns),'impact':texts,
        'proposal_confirmed':False,'model_requests':0,'screenshot':str(screenshot),
        'screenshot_sha256':hashlib.sha256(screenshot.read_bytes()).hexdigest(),
        'deterministic_size':{'selected':selected_label,'full_column_rerun':352,'estimated_model_requests':0,
                              'screenshot':str(deterministic_shot),'screenshot_sha256':hashlib.sha256(deterministic_shot.read_bytes()).hexdigest()},
        'page_errors':errors}
path=args.out/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'report':str(path),'source_jobs':source_jobs,'repair_rows':total,'columns':sorted(columns)},ensure_ascii=False))
