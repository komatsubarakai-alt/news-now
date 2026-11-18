"""Offline UI regression and controlled operation-count comparison, synthetic only."""
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright
import operator_app as app
import operator_preview as preview
from operator_task_demo import populate

def run():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True);parser.add_argument('--baseline-template',type=Path);args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    if args.output_dir.resolve().is_relative_to(root):raise ValueError('output must be outside repository')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    result=dict(scope='fictional_offline',checks=[],requests=[],errors=[],actual_android='unverified',actual_keyboard='unverified',real_operator_time='unmeasured')
    def check(label,ok=True):
        if not ok:raise AssertionError(label)
        result['checks'].append(label)
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'preview.html';preview.build(path,demo=True,review_reuse_example=True);html=path.read_text();encoded=re.search(r'<script id="offlineSnapshot" type="application/json">(.*?)</script>',html,re.S).group(1);state=json.loads(encoded)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
            context=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
            context.route('**/*',lambda r:(result['requests'].append(r.request.url),r.abort()))
            def screen(content):
                page=context.new_page();page.on('pageerror',lambda e:result['errors'].append(str(e)));page.set_content(content);page.wait_for_function('()=>state!==null');page.evaluate("()=>openTask(state.work.tasks.find(t=>t.article?.url.endsWith('demo-02')))");return page
            page=screen(html)
            check('no automatic identity selection',page.evaluate('choice.relation===null'))
            page.locator('.support-summary > summary').click();check('rule candidate clearly non-final',page.get_by_text('ルールによる参考情報・未確定',exact=True).is_visible())
            for heading in ['同じ事件と考えられる材料','別事件の可能性・相違点','まだ確認できていない情報','次に確認すること']:check(heading,page.get_by_role('heading',name=heading,exact=True).is_visible())
            proof=page.locator('.support-summary details').filter(has=page.locator('summary').filter(has_text='両側の保存引用を確認')).first;proof.locator('summary').first.click();check('saved quotes open without fetching',proof.locator('blockquote').count()>0 and proof.locator('a').count()>0)
            check('missing occurrence remains unknown',page.evaluate("task(view.taskId).drafts_by_event[choice.eid].materials.unknown.some(p=>p.field==='occurred_at')"))
            check('different quotation not automatically contradiction',page.evaluate("task(view.taskId).drafts_by_event[choice.eid].materials.fact_status==='未確認'"))
            def act(pg):pg.get_by_role('button',name='同じ事件',exact=True).click();pg.get_by_role('button',name='同じ事件の続報・新しい進展あり',exact=True).click()
            act(page);page.get_by_role('button',name='前回の入力を再利用する（要確認）',exact=True).click()
            check('saved input copied explicitly',page.get_by_label('以前の記録との違い（確認記録・未検証）',exact=True).input_value().startswith('【架空の保存済み入力】'))
            check('copy never confirms evidence',page.evaluate('choice.evidence_ids.length===0'));check('copy never confirms duplicate review',page.evaluate('choice.duplicate_checked===false'))
            check('copy provenance retained',page.evaluate("choice.copied_review_op==='fictional-saved-input-example'"));check('readonly save remains disabled',page.get_by_role('button',name='閲覧専用：記録は保存できません',exact=True).is_disabled())
            for width,height in [(360,800),(393,851),(412,851),(360,380)]:
                page.set_viewport_size({'width':width,'height':height});check(f'{width}x{height} no horizontal overflow',page.evaluate('document.documentElement.scrollWidth<=innerWidth'));page.get_by_label('判断・保留の理由（自由記述・追加説明）',exact=True).scroll_into_view_if_needed();check(f'{width}x{height} footer does not overlay input',page.evaluate("getComputedStyle(document.querySelector('.bar')).position==='static'"))
            page.set_viewport_size({'width':393,'height':851});page.screenshot(path=str(args.output_dir/'reference-mobile.png'),full_page=True)
            for n,eid in [('04',None),('03',None),('10','demo-e2'),('10','demo-e3')]:
                page.evaluate("([n,eid])=>openTask(state.work.tasks.find(t=>t.article?.url.endsWith('demo-'+n)&&(!eid||t.required_event_id===eid)))",[n,eid]);check('materials available '+n+str(eid),page.evaluate('!!task(view.taskId).drafts_by_event[choice.eid].materials'))
                if n=='04':check('missing data not labelled differences',page.evaluate('task(view.taskId).drafts_by_event[choice.eid].materials.differences.length===0'))
                if n=='10':check('other incident review not offered '+eid,page.get_by_role('button',name='前回の入力を再利用する（要確認）',exact=True).count()==0)
            if args.baseline_template:
                old=args.baseline_template.read_text().replace('<script id="offlineSnapshot" type="application/json">null</script>','<script id="offlineSnapshot" type="application/json">'+encoded+'</script>').replace('__LOCAL_TOKEN__','offline-read-only')
                measures={};target=next(t for t in state['work']['tasks'] if t.get('article',{}).get('url','').endswith('demo-02'));saved=target['drafts_by_event'][target['suggested_event_id']]['previous_input']
                for label,content in [('before',old),('after',html)]:
                    pg=screen(content);act(pg);actions=2
                    if label=='before':
                        pg.get_by_role('button',name='下書きを入力欄に取り込む（未確認）',exact=True).click();actions+=1
                        pg.get_by_label('以前の記録との違い（確認記録・未検証）',exact=True).fill(saved['difference']);actions+=1
                        for code in saved['reason_codes']:pg.get_by_label(code,exact=True).check();actions+=1
                        pg.get_by_label('判断・保留の理由（自由記述・追加説明）',exact=True).fill(saved['reason_text']);actions+=1
                    else:pg.get_by_role('button',name='前回の入力を再利用する（要確認）',exact=True).click();actions+=1
                    pg.locator('.evidence-article[open] label.check').first.locator('input').check();pg.get_by_label('既に記録済みの進展との重複を確認した（自動確定しません）',exact=True).check();actions+=2
                    values=pg.evaluate("({fact:choice.new_fact,difference:choice.difference,stage:choice.proposed_stage,codes:choice.reason_codes,reason:choice.reason,evidence:choice.evidence_ids,duplicate:choice.duplicate_checked})")
                    measures[label]={'control_actions':actions,'values':values};pg.close()
                check('same final field values in controlled before-after',measures['before']['values']==measures['after']['values']);result['operation_comparison']=dict(before=measures['before']['control_actions'],after=measures['after']['control_actions'],scope='one fictional revisit, includes identity/progression clicks and evidence/duplicate checks; excludes reading, scrolling, typing keystrokes, navigation and save',real_user_timing='unmeasured')
            check('no external requests',not result['requests']);check('no page errors',not result['errors']);browser.close()
        if args.baseline_template:
            # One identical temporary DB and task are read-only inputs to both code versions.
            db=app.connect();app.initialize(db);populate(db);s=app.snapshot(db);t=next(t for t in s['work']['tasks'] if t.get('article',{}).get('url','').endswith('demo-02'));eid=t['suggested_event_id'];d=t['drafts_by_event'][eid]
            app.operate(db,dict(action='task_review',op='timing-reference',revision=s['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=eid,relation='same_event',progress='new',proposed_stage=d['proposed_stage'],reason='架空の比較入力',draft_review={'draft_id':d['draft_id'],'applied_fields':[]},progress_details=dict(new_fact=d['new_fact'],difference='架空の以前との差',evidence_ids=[],duplicate_checked=False)))
            s=app.snapshot(db);t=next(t for t in s['work']['tasks'] if t.get('article',{}).get('url','').endswith('demo-02'));e=next(i['event'] for i in s['items'] if i['event']['event_id']==eid)
            db_path=Path(tmp)/'timing.sqlite';dest=sqlite3.connect(db_path);db.backup(dest);dest.close();db.close();inputs=Path(tmp)/'inputs.json';inputs.write_text(json.dumps([t,e]));dbhash=hashlib.sha256(db_path.read_bytes()).hexdigest()
            code="""import sys,json,sqlite3,time,statistics
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1]).parents[1])]
import operator_support
db=sqlite3.connect('file:'+sys.argv[2]+'?mode=ro',uri=True);t,e=json.loads(Path(sys.argv[3]).read_text());times=[]
with patch('socket.socket.connect',side_effect=AssertionError('network prohibited')):
 for _ in range(25):
  start=time.perf_counter();operator_support.build(db,t,e);times.append((time.perf_counter()-start)*1000)
print(json.dumps({'median_ms':statistics.median(times),'repetitions':len(times)}))
"""
            timing={}
            for label,module_dir in [('before',args.baseline_template.parent),('after',Path(__file__).parent)]:timing[label]=json.loads(subprocess.check_output([sys.executable,'-c',code,str(module_dir),str(db_path),str(inputs)],text=True))
            timing.update(scope='single fictional task, pure draft projection only, same read-only SQLite and task input; excludes startup/render/network',input_sha256=hashlib.sha256(inputs.read_bytes()).hexdigest(),db_unchanged=dbhash==hashlib.sha256(db_path.read_bytes()).hexdigest());result['timing_comparison']=timing
    (args.output_dir/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':run()
