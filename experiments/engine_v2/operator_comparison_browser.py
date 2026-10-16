"""Offline synthetic mobile UI checks. Viewport reduction is not a real keyboard."""
import argparse
import json
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
import operator_preview

def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True,type=Path);args=parser.parse_args()
    if args.output_dir.resolve().is_relative_to(Path(__file__).resolve().parents[2]):raise ValueError('output must be outside repository')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    result=dict(scope='synthetic_read_only_mobile_emulation',checks=[],page_errors=[],requests=[],actual_android='unverified',real_keyboard='unverified',file_navigation='unverified; HTML set_content used')
    def check(name,ok=True):
        if not ok:raise AssertionError(name)
        result['checks'].append(name)
    with tempfile.TemporaryDirectory() as tmp:
        html=Path(tmp)/'preview.html';operator_preview.build(html,demo=True)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
            context.route('**/*',lambda r:(result['requests'].append(r.request.url),r.abort()))
            page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
            page.set_content(html.read_text());page.get_by_role('heading',name='今日の確認作業',exact=True).wait_for()
            original=page.evaluate('JSON.stringify(state)')
            def open_case(number,eid=None):
                page.evaluate("([n,eid])=>{const t=state.work.tasks.find(t=>t.article?.url.endsWith('demo-'+n)&&(!eid||t.required_event_id===eid));openTask(t)}",[number,eid])
                page.get_by_role('heading',name='第1段階：同じ事件ですか？',exact=True).wait_for()
            def same(progress):
                page.get_by_role('button',name='同じ事件',exact=True).click();page.get_by_role('button',name=progress,exact=True).click()
            for number in ['01','02','03','04','05','06','07','08','09','10']:
                open_case(number,'demo-e2' if number=='10' else None)
                check('case '+number+' comparison immediately precedes decision',page.evaluate("document.querySelector('.comparison').nextElementSibling.querySelector('h3').textContent==='第1段階：同じ事件ですか？'"))
                check('case '+number+' 5 comparison fields plus facts',page.locator('.compare-table tbody tr').count()==6)
            open_case('02');same('同じ事件の続報・新しい進展あり')
            for label in ['新しく判明した事実（引用の原文をそのまま入力）','以前の記録との違い（確認記録・未検証）','出来事の発生日時（日本時間・原文で確認できた場合のみ）']:
                check(label,page.get_by_label(label,exact=True).is_visible())
            page.get_by_label('記事に書かれた進展段階（引用で確認してください）',exact=True).select_option('起訴')
            page.get_by_label('新しく判明した事実（引用の原文をそのまま入力）',exact=True).fill('起訴')
            page.get_by_label('以前の記録との違い（確認記録・未検証）',exact=True).fill('逮捕から起訴へ')
            check('duplicate check visible',page.get_by_label('既に記録済みの進展との重複を確認した（自動確定しません）',exact=True).is_visible())
            check('readonly save disabled',page.get_by_role('button',name='閲覧専用：記録は保存できません',exact=True).is_disabled())
            for width,height in [(360,800),(393,851),(412,851),(360,380)]:
                page.set_viewport_size({'width':width,'height':height});check(f'{width}x{height} no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                field=page.get_by_label('判断・保留の理由（自由記述・追加説明）',exact=True);field.focus();field.scroll_into_view_if_needed()
                check(f'{width}x{height} no fixed footer overlay',page.evaluate("getComputedStyle(document.querySelector('.bar')).position==='static'"))
            page.set_viewport_size({'width':393,'height':851})
            page.screenshot(path=str(args.output_dir/'comparison-progress-mobile.png'),full_page=True)
            for relation,progress,expected,unexpected in [('別の事件',None,'発生場所が異なる','既報と同じ内容'),('判断できない・保留',None,'引用根拠が不足','発生場所が異なる'),('同じ事件','同じ事件だが新しい進展なし','既報と同じ内容','発生場所が異なる')]:
                open_case('01');page.get_by_role('button',name=relation,exact=True).click()
                if progress:page.get_by_role('button',name=progress,exact=True).click()
                check('reason group '+relation+str(progress),page.get_by_label(expected,exact=True).is_visible() and page.get_by_label(unexpected,exact=True).count()==0)
                options=page.locator('fieldset input');options.nth(0).check();options.nth(1).check();check('multi select '+relation,options.nth(0).is_checked() and options.nth(1).is_checked())
                page.get_by_role('button',name='判断できない・保留' if relation!='判断できない・保留' else '別の事件',exact=True).click();check('reasons cleared on changed decision '+relation,page.evaluate('choice.reason_codes.length===0'))
            open_case('08');check('body missing visible',page.get_by_text('本文未確認',exact=True).is_visible())
            open_case('05');check('missing quote stops visibly',page.get_by_text('停止：有効な引用根拠が不足しています',exact=True).is_visible())
            open_case('09');check('revised edition warning',page.get_by_text('再確認：記事の内容が変更されています',exact=True).is_visible())
            open_case('07');same('過去の報道内容の訂正・撤回の可能性あり');check('one consolidated correction warning',page.get_by_text('重要：訂正・撤回の可能性があります',exact=True).count()==1)
            open_case('06');same('同じ事件の続報・新しい進展あり');check('duplicate indictment shown',page.get_by_text('記録済みの進展：',exact=False).inner_text().find('起訴')>=0)
            for eid,person,other in [('demo-e2','被害者山田','被害者佐藤'),('demo-e3','被害者佐藤','被害者山田')]:
                open_case('10',eid);check('shared incident '+eid,page.locator('.compare-table').inner_text().find(person)>=0 and other not in page.locator('.compare-table').inner_text());check('shared target locked '+eid,page.get_by_label('比較する追跡中の事件',exact=True).is_disabled())
            check('no state, approvals, or counters changed',original==page.evaluate('JSON.stringify(state)'))
            open_case('02');page.evaluate("()=>{const t=task(view.taskId);t.article.title='長い架空事件名'.repeat(80);item(choice.eid).event.event_name='非常に長い架空の事件名'.repeat(80);render()}")
            check('long names wrap',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            page.get_by_role('button',name='← 元の一覧へ',exact=True).click();check('back to task list',page.get_by_role('heading',name='事件の判定待ち',exact=True).is_visible())
            check('no page errors',not result['page_errors']);check('no network requests',not result['requests']);browser.close()
    (args.output_dir/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':run()
