"""Loopback-only mobile Chromium checks, synthetic data, no real Android claims."""
import argparse
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path);args=parser.parse_args()
script=Path(__file__).with_name('operator_app.py').resolve();out=(args.output_dir or Path(tempfile.mkdtemp(prefix='operator-ui-browser-'))).resolve()
if out.is_relative_to(script.parents[2]):raise ValueError('browser results must be outside repository')
out.mkdir(parents=True,exist_ok=True)
results={'browser':'Chromium mobile emulation','actual_android':'unverified','checks':[],'cases':[],'outside_requests':[],'page_errors':[],'file_scheme':'environment policy prevents direct file navigation; tested generated HTML using set_content'}

def check(name,condition=True):
    if not condition:raise AssertionError(name)
    results['checks'].append(name)

with tempfile.TemporaryDirectory(prefix='operator-ui-browser-fixture-') as tmp:
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    proc=subprocess.Popen([sys.executable,str(script),'--demo','--db',tmp+'/app.sqlite','--port',str(port)],stdout=subprocess.PIPE,text=True)
    try:
        address=proc.stdout.readline().strip().split()[-1]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
            context=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
            def route(r):
                if r.request.url.startswith(address+'/'):r.continue_()
                else:results['outside_requests'].append(r.request.url);r.abort()
            context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:results['page_errors'].append(str(e)))
            page.goto(address);page.get_by_role('heading',name='今日の確認作業',exact=True).wait_for()
            initial=page.evaluate('state');check('dashboard counts equal unique unfinished backend tasks',initial['work']['counts']=={'identity':7,'progress':0,'publication':1,'hold':4})
            for width in (360,393,412):
                page.set_viewport_size({'width':width,'height':851});check(str(width)+'px home without horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            check('compact fixed header at most 60px',page.locator('header').bounding_box()['height']<=60)
            check('home action tiles at least 48px',page.locator('.tile').first.bounding_box()['height']>=48)
            page.screenshot(path=str(out/'home-mobile.png'),full_page=True)
            def home():
                page.wait_for_function('()=>state!==null');page.get_by_role('button',name='今日の作業へ',exact=True).click()
            def open_public():
                home();page.locator('.tile').filter(has_text='公開の確認待ち').click();page.get_by_role('button',name='確認する',exact=True).click()
            open_public();page.get_by_role('button',name='安全検証を通せる未承認項目を選択',exact=True).click()
            page.get_by_role('button',name='選んだ内容の公開前プレビュー',exact=True).click();page.locator('#previewDialog').wait_for(state='visible')
            check('bundle preview creates no approvals',all(t['status']=='unapproved' for t in page.evaluate("state.items.find(i=>i.event.event_id==='demo-e1').targets")))
            page.get_by_role('button',name='閉じる',exact=True).click()
            page.get_by_role('button',name='選んだ内容をまとめて最終確認',exact=True).click();page.get_by_role('button',name='戻る',exact=True).click();check('cancel confirmation sends nothing',not page.evaluate('state.approved_preview.length'))
            page.get_by_role('button',name='選んだ内容をまとめて最終確認',exact=True).click();page.locator('#confirmReason').fill('架空の内容・根拠・対象を確認');page.get_by_role('button',name='確認して実行',exact=True).dblclick();page.wait_for_function('()=>!busy')
            check('bundle approves each item with individual results',len(page.evaluate('lastResult.results'))==8);check('publication badge is one work, then zero',page.evaluate('state.work.counts.publication')==0)
            target=page.locator('article').filter(has_text='事件名・分類 ／ 承認済み');target.get_by_role('button',name='この内容の公開を取り消す',exact=True).click();page.locator('#confirmReason').fill('架空の公開取消し');page.get_by_role('button',name='確認して実行',exact=True).click();page.wait_for_function('()=>!busy');check('withdrawal removes public event',not page.evaluate('state.approved_preview.length'))
            page.get_by_role('button',name='安全検証を通せる未承認項目を選択',exact=True).click();page.get_by_role('button',name='選んだ内容をまとめて最終確認',exact=True).click();page.locator('#confirmReason').fill('架空の再承認');page.get_by_role('button',name='確認して実行',exact=True).click();page.wait_for_function('()=>!busy');check('explicit reapproval restores event',page.evaluate('state.approved_preview.length')==1)
            baseline=page.evaluate('state.items.map(i=>i.event)')
            def open_case(number,eid=None):
                home();t=page.evaluate("([number,eid])=>state.work.tasks.find(t=>t.article?.url.endsWith('demo-'+number)&&(!eid||t.required_event_id===eid))",[number,eid])
                page.locator('.tile').filter(has_text={'identity':'事件の判定待ち','progress':'続報・進展の確認待ち','hold':'保留・再確認が必要'}[t['category']]).click()
                card=page.locator('article').filter(has=page.get_by_role('heading',name=t['article']['title'],exact=True))
                if eid:
                    name=next(i['event']['event_name'] for i in page.evaluate('state.items') if i['event']['event_id']==eid);card=card.filter(has_text='確認対象：'+name)
                card.get_by_role('button',name='確認する',exact=True).click();page.get_by_role('heading',name='第1段階：同じ事件ですか？',exact=True).wait_for();return t
            def decide(relation,progress=None,stage=None):
                page.get_by_role('button',name=relation,exact=True).click()
                if progress:page.get_by_role('button',name=progress,exact=True).click()
                if stage:page.get_by_label('記事に書かれた進展段階（引用で確認してください）').select_option(stage)
                page.locator('main textarea').fill('架空データの根拠を確認。実ニュースの正解ではありません。')
                page.get_by_role('button',name='確認記録を保存して次へ',exact=True).click();page.wait_for_function('()=>!busy')
            open_case('01');check('second step hidden until same event',not page.get_by_role('heading',name='第2段階：新しい進展がありますか？',exact=True).count());check('Japanese publication time',page.get_by_text('2026年10月8日 20:00',exact=False).count()>0);check('occurrence time remains unknown',page.get_by_text('出来事の発生日時：未確認。記事の公開日時とは区別します。',exact=True).is_visible())
            page.locator('main textarea').fill('再読込後も残る理由の下書き');page.reload();open_case('01');check('draft retained across reload',page.locator('main textarea').input_value()=='再読込後も残る理由の下書き')
            for width in (360,393,412):
                page.set_viewport_size({'width':width,'height':851});check(str(width)+'px review without horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            page.screenshot(path=str(out/'review-mobile.png'),full_page=True);results['review_height_px']=page.evaluate('document.documentElement.scrollHeight')
            decide('同じ事件','同じ事件だが新しい進展なし');check('completed review removed from unfinished count',page.evaluate('state.work.completed.length')==1);results['cases'].append('same event, no new progression')
            open_case('02');decide('同じ事件','同じ事件の続報・新しい進展あり','起訴');check('arrest-to-indictment recorded without adoption',page.evaluate('lastResult.progress_assessment')=='operator_reported_new');results['cases'].append('arrest to indictment, reference only')
            open_case('03');decide('別の事件');results['cases'].append('different incident')
            open_case('04');decide('判断できない・保留');results['cases'].append('uncertain identity held')
            open_case('05');decide('同じ事件','同じ事件の続報・新しい進展あり','起訴');check('missing quote fails safely',page.locator('#notice').inner_text().find('引用')>=0);decide('判断できない・保留');results['cases'].append('missing progression evidence held')
            open_case('06');decide('同じ事件','同じ事件の続報・新しい進展あり','起訴');check('duplicate quotation is not counted as new progress',page.evaluate('lastResult.progress_assessment')=='already_recorded_literal_candidate');results['cases'].append('duplicate indictment report')
            open_case('07');decide('同じ事件','過去の報道内容の訂正・撤回の可能性あり');check('correction recorded separately',page.evaluate('lastResult.correction')=='possible');results['cases'].append('possible correction, separate alert')
            open_case('08');check('body missing explicitly displayed',page.get_by_text('本文未確認',exact=True).count()>0);check('title match not body verification',page.get_by_text('見出し一致・原文未確認',exact=True).count()>0);decide('判断できない・保留');results['cases'].append('body not retrieved')
            open_case('09');check('article revision warning visible',page.get_by_text('記事の更新が記録されています。以前の判断はそのまま使えません。',exact=True).is_visible());decide('同じ事件','同じ事件だが新しい進展なし');results['cases'].append('article edition recheck')
            open_case('10','demo-e2');check('shared article warning visible',page.get_by_text('この1記事は複数の事件に対応しています。記事が同じでも事件は統合しません。対象の事件ごとに確認してください。',exact=True).is_visible());decide('同じ事件','同じ事件だが新しい進展なし');open_case('10','demo-e3');decide('判断できない・保留');results['cases'].append('shared article, independent incident records')
            check('all ten UI paths leave event facts and progression counts unchanged',page.evaluate('state.items.map(i=>i.event)')==baseline)
            page.get_by_role('button',name='確認済み・履歴',exact=True).click();check('completed and pending screens separated',page.get_by_role('heading',name='確認済み・履歴',exact=True).is_visible())
            completed=page.locator('article').filter(has=page.get_by_role('heading',name='江別豊幌住宅強盗 被害者山田 逮捕の経緯を改めて報道',exact=True))
            page.once('dialog',lambda d:d.accept('架空の判断を見直します'))
            completed.get_by_role('button',name='この判断を見直す',exact=True).click();page.wait_for_function('()=>!busy');check('explicit reconsideration returns completed work to hold',page.evaluate("state.work.tasks.some(t=>t.article?.url.endsWith('demo-01')&&t.category==='hold')"))
            page.get_by_role('button',name='確認済み・履歴',exact=True).click()
            with page.expect_download() as dl:page.get_by_role('button',name='確認記録をJSONで保存',exact=True).click()
            dl.value.save_as(tmp+'/records.json');check('record export explicitly not authority',json.loads(Path(tmp+'/records.json').read_text())['scope']=='isolated_review_only_not_authority')
            fixture=next(t for t in initial['work']['tasks'] if t.get('article',{}).get('url','').endswith('demo-01'))
            page.evaluate("fixture=>{state.work.tasks=Array.from({length:35},(_,i)=>({...fixture,task_id:'pagination-'+i,category:'identity'}));state.work.counts={identity:35,progress:0,publication:0,hold:0};go({page:'home'});}",fixture)
            page.locator('.tile').filter(has_text='事件の判定待ち').click();check('list initially shows 20 synthetic UI tasks',page.locator('main article').count()==20)
            page.get_by_role('button',name='次の20件',exact=True).click();check('pagination retains and shows remaining 15 tasks',page.locator('main article').count()==15)
            page.get_by_role('button',name='前の20件',exact=True).click();check('pagination returns to first page',page.locator('main article').count()==20)
            page.reload();home()
            bad=context.request.post(address+'/api/operate',data='{}',headers={'Origin':'https://invalid.example','Content-Type':'application/json'});check('foreign origin blocked',bad.status==403)
            bad=context.request.get(address+'/api/state',headers={'Host':'invalid.example'});check('foreign host blocked',bad.status==403)
            sys.path.insert(0,str(script.parent));import operator_preview
            html=Path(tmp)/'offline.html';operator_preview.build(html,demo=True)
            offline=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
            def block(r):results['outside_requests'].append(r.request.url);r.abort()
            offline.route('**/*',block);opage=offline.new_page();opage.on('pageerror',lambda e:results['page_errors'].append(str(e)));opage.set_content(html.read_text())
            opage.get_by_role('heading',name='今日の確認作業',exact=True).wait_for();check('offline clearly read-only',opage.locator('#mode').inner_text()=='閲覧専用・架空データ');opage.locator('.tile').filter(has_text='公開の確認待ち').click();opage.get_by_role('button',name='確認する',exact=True).click();check('offline approval disabled',opage.get_by_role('button',name='選んだ内容をまとめて最終確認',exact=True).is_disabled())
            offline.close()
            import operator_handoff
            handoff=operator_handoff.build(html,Path(tmp)/'handoff');download_page=context.new_page();download_page.on('pageerror',lambda e:results['page_errors'].append(str(e)))
            download_page.goto(address);download_page.get_by_role('heading',name='今日の確認作業',exact=True).wait_for();download_page.set_content((Path(tmp)/'handoff/save-preview.html').read_text())
            with download_page.expect_download() as restored:download_page.get_by_role('button',name='閲覧用HTMLを保存',exact=True).click()
            restored.value.save_as(tmp+'/restored.html');check('offline download wrapper restores exact HTML with SHA-256',Path(tmp+'/restored.html').read_bytes()==html.read_bytes());download_page.close()
            check('no external page requests',not results['outside_requests']);check('no JavaScript errors',not results['page_errors']);browser.close()
    finally:proc.terminate();proc.wait(timeout=5)
(out/'browser_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps(results,ensure_ascii=False))
