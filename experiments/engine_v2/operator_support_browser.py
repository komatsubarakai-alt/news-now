"""Synthetic loopback + offline UI tests. No remote news or AI requests."""
import argparse
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
import operator_preview

def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',required=True,type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    if args.output_dir.resolve().is_relative_to(root):raise ValueError('output must be outside repository')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    result=dict(scope='synthetic_loopback_and_readonly',checks=[],outside_requests=[],page_errors=[],actual_android='unverified',real_keyboard='unverified',file_navigation='unverified; offline HTML set_content used')
    def check(name,ok=True):
        if not ok:raise AssertionError(name)
        result['checks'].append(name)
    with tempfile.TemporaryDirectory() as tmp:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close();db=Path(tmp)/'operator.sqlite'
        proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('operator_app.py')),'--demo','--db',str(db),'--port',str(port)],stdout=subprocess.PIPE,text=True)
        try:
            address=proc.stdout.readline().strip().split()[-1]
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
                context=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
                controls={'lose_response':False,'operations':0}
                def route(r):
                    if not r.request.url.startswith(address+'/'):result['outside_requests'].append(r.request.url);r.abort();return
                    if r.request.url.endswith('/api/operate'):
                        controls['operations']+=1
                        if controls['lose_response']:
                            controls['lose_response']=False;r.fetch();r.abort();return
                    r.continue_()
                context.route('**/*',route);page=context.new_page();page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
                page.goto(address);page.get_by_role('heading',name='今日の確認作業',exact=True).wait_for();baseline=page.evaluate('JSON.stringify(state.items.map(i=>i.event))')
                def open_case(n,eid=None):
                    page.evaluate("([n,eid])=>openTask(state.work.tasks.find(t=>t.article?.url.endsWith('demo-'+n)&&(!eid||t.required_event_id===eid)))",[n,eid])
                    page.get_by_role('heading',name='第1段階：同じ事件ですか？',exact=True).wait_for()
                def choose(progress):
                    page.get_by_role('button',name='同じ事件',exact=True).click();page.get_by_role('button',name=progress,exact=True).click()
                open_case('02');check('proposal never selects identity',page.evaluate('choice.relation===null'))
                page.locator('.support-summary > summary').click();check('saved simulated AI reuse explicitly identified',page.locator('.support-summary').inner_text().find('今回のAPI通信はありません')>=0)
                choose('同じ事件の続報・新しい進展あり');page.get_by_role('button',name='下書きを入力欄に取り込む（未確認）',exact=True).click()
                fact=page.get_by_label('新しく判明した事実（引用の原文をそのまま入力）',exact=True);difference=page.get_by_label('以前の記録との違い（確認記録・未検証）',exact=True)
                check('explicit apply copies proposed fact',bool(fact.input_value()));check('stage proposed but not adopted',page.get_by_label('記事に書かれた進展段階（引用で確認してください）',exact=True).input_value()=='起訴')
                check('applying draft does not select quotes',page.evaluate('choice.evidence_ids.length===0'));check('applying draft does not confirm duplicates',not page.get_by_label('既に記録済みの進展との重複を確認した（自動確定しません）',exact=True).is_checked())
                difference.fill('運営者が修正した比較');page.get_by_label('新しい事実を引用で確認',exact=True).check();page.get_by_label('以前の記録との差を確認',exact=True).check()
                page.get_by_role('button',name='同じ事件だが新しい進展なし',exact=True).click();page.get_by_label('既報と同じ内容',exact=True).check();page.get_by_label('新しい事実がない',exact=True).check()
                reason=page.get_by_label('判断・保留の理由（自由記述・追加説明）',exact=True);reason.fill('進展なしの場合の理由')
                page.get_by_role('button',name='別の事件',exact=True).click();page.get_by_label('発生場所が異なる',exact=True).check();check('no progression controls for different event',page.get_by_role('heading',name='第2段階：新しい進展がありますか？',exact=True).count()==0)
                page.get_by_role('button',name='判断できない・保留',exact=True).click();page.get_by_label('引用根拠が不足',exact=True).check()
                page.get_by_role('button',name='同じ事件',exact=True).click();check('last same-event progression preserved',page.evaluate("choice.progress==='none'"));check('none reasons restored',page.get_by_label('既報と同じ内容',exact=True).is_checked() and page.get_by_label('新しい事実がない',exact=True).is_checked())
                page.get_by_role('button',name='同じ事件の続報・新しい進展あり',exact=True).click();check('edited proposal preserved across switches',difference.input_value()=='運営者が修正した比較');check('new-progress reasons restored',page.get_by_label('新しい事実を引用で確認',exact=True).is_checked())
                for label in ['進展の有無が不明・要確認','過去の報道内容の訂正・撤回の可能性あり']:
                    page.get_by_role('button',name=label,exact=True).click();check('only required fields for '+label,page.get_by_label('新しく判明した事実（引用の原文をそのまま入力）',exact=True).count()==0)
                page.get_by_role('button',name='同じ事件の続報・新しい進展あり',exact=True).click();check('new fields survive unknown and correction',difference.input_value()=='運営者が修正した比較')
                page.reload();page.wait_for_function('()=>state!==null');open_case('02');check('revision-scoped input restored after reload',difference.input_value()=='運営者が修正した比較')
                for width,height in [(360,800),(393,851),(412,851),(360,380)]:
                    page.set_viewport_size({'width':width,'height':height});check(f'{width}x{height} no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'));difference.focus();difference.scroll_into_view_if_needed();check(f'{width}x{height} footer not fixed over input',page.evaluate("getComputedStyle(document.querySelector('.bar')).position==='static'"))
                page.set_viewport_size({'width':393,'height':851});page.screenshot(path=str(args.output_dir/'support-mobile.png'),full_page=True)
                check('article cards include source state separately',page.locator('.evidence-article').filter(has_text='出典の確認：未検証').count()>0);check('occurrence never inferred',page.get_by_text('出来事の発生日時：未確認',exact=True).count()>0)
                page.locator('.evidence-article[open] label.check').first.locator('input').check();page.get_by_label('既に記録済みの進展との重複を確認した（自動確定しません）',exact=True).check();page.get_by_label('判断・保留の理由（自由記述・追加説明）',exact=True).fill('保存通信の障害を検証')
                controls['lose_response']=True;page.get_by_role('button',name='確認記録を保存して次へ',exact=True).click();page.wait_for_function('()=>!busy')
                check('lost response never shown as success','安全のため停止しました' in page.locator('#notice').inner_text());check('uncertain save retains input',difference.input_value()=='運営者が修正した比較')
                count=controls['operations'];page.get_by_role('button',name='確認記録を保存して次へ',exact=True).click();check('uncertain save cannot create new operation',controls['operations']==count)
                page.get_by_role('button',name='未完了の操作を同じ番号で確認',exact=True).click();page.wait_for_function('()=>!busy');check('retry uses original receipt',controls['operations']==count+1)
                con=sqlite3.connect(db);check('one review after lost response and retry',con.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0]==1)
                audit=json.loads(con.execute('SELECT payload FROM ui_review_details').fetchone()[0]);check('original proposal and edited operator values preserved',audit['draft_review']['original']['difference']!=audit['draft_review']['operator_values']['difference']);con.close()
                check('save does not alter event facts',baseline==page.evaluate('JSON.stringify(state.items.map(i=>i.event))'))
                page.evaluate("()=>{sessionStorage.setItem('news-now-work-pending',JSON.stringify({action:'task_review',op:'obsolete-ui-operation',revision:'obsolete',event_id:null}));render()}")
                page.get_by_role('button',name='未完了の操作を同じ番号で確認',exact=True).click();page.wait_for_function('()=>!busy');check('definitive retry rejection is not success','安全のため停止しました' in page.locator('#notice').inner_text());check('definitive rejection releases pending operation',page.evaluate("sessionStorage.getItem('news-now-work-pending')===null"))

                open_case('10','demo-e2');choose('同じ事件の続報・新しい進展あり');difference.fill('江別側だけの入力');open_case('10','demo-e3');choose('同じ事件の続報・新しい進展あり');check('shared event inputs not mixed',difference.input_value()=='');open_case('10','demo-e2');check('shared event own input restored',difference.input_value()=='江別側だけの入力')
                html=Path(tmp)/'readonly.html';operator_preview.build(html,demo=True);off=browser.new_context(viewport={'width':360,'height':800},is_mobile=True,has_touch=True);off.route('**/*',lambda r:(result['outside_requests'].append(r.request.url),r.abort()));op=off.new_page();op.on('pageerror',lambda e:result['page_errors'].append(str(e)));op.set_content(html.read_text());op.wait_for_function('()=>state!==null')
                op.evaluate("()=>openTask(state.work.tasks.find(t=>t.article?.url.endsWith('demo-02')))");op.get_by_role('button',name='同じ事件',exact=True).click();op.get_by_role('button',name='同じ事件の続報・新しい進展あり',exact=True).click();op.get_by_role('button',name='下書きを入力欄に取り込む（未確認）',exact=True).click();check('readonly draft editable but save disabled',op.get_by_role('button',name='閲覧専用：記録は保存できません',exact=True).is_disabled())
                op.evaluate("()=>{const t=task(view.taskId);t.article.title='長い架空見出し'.repeat(70);item(choice.eid).event.event_name='長い架空事件名'.repeat(70);t.drafts_by_event[choice.eid].evidence_cards[0].quote='長い架空引用'.repeat(100);render()}");check('long names titles quotes wrap',op.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                op.get_by_label('以前の記録との違い（確認記録・未検証）',exact=True).fill('旧版の入力');op.evaluate("()=>{const t=task(view.taskId);t.review_contexts[choice.eid]='simulated-new-edition';t.drafts_by_event[choice.eid].draft_id='simulated-new-proposal';render()}");check('simulated changed context does not restore stale input',op.evaluate("choice.difference===''"))
                check('no page errors',not result['page_errors']);check('no outside requests',not result['outside_requests']);browser.close()
        finally:proc.terminate();proc.wait(timeout=5)
    (args.output_dir/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':run()
