import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
import argparse
parser=argparse.ArgumentParser(description='Local-only mobile browser rehearsal; requires Playwright and Chromium')
parser.add_argument('--output-dir',type=Path);args=parser.parse_args()
app=Path(__file__).with_name('operator_app.py').resolve()
base=(args.output_dir or Path(tempfile.mkdtemp(prefix='operator-browser-results-'))).resolve()
if base.is_relative_to(app.parents[2]):raise ValueError('results must be outside repository')
base.mkdir(parents=True,exist_ok=True)
result={'browser':'Chromium mobile emulation','actual_android':'unverified','file_scheme_navigation':'blocked by environment browser policy; HTML tested with set_content','checks':[],'outside_requests':[]}
with tempfile.TemporaryDirectory(prefix='operator-browser-') as tmp:
 sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
 server=subprocess.Popen([sys.executable,str(app),'--db',tmp+'/operator.sqlite','--demo','--port',str(port)],stdout=subprocess.PIPE,text=True)
 try:
  address=server.stdout.readline().strip().split()[-1]
  with sync_playwright() as p:
   browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
   context=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
   def route(r):
    if r.request.url.startswith(address+'/'):r.continue_()
    else:result['outside_requests'].append(r.request.url);r.abort()
   context.route('**/*',route);page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
   page.goto(address);page.wait_for_function("()=>document.getElementById('counts').textContent.includes('待機・判定記録')")
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');result['checks'].append('393px list no horizontal overflow')
   assert page.get_by_role('button',name='保留',exact=True).count()==1
   page.locator('article textarea').first.fill('本文未確認');page.get_by_role('button',name='保留',exact=True).click();page.get_by_text('隔離SQLiteへ保存しました',exact=False).wait_for();result['checks'].append('queue hold saved')
   page.get_by_role('button',name='記事・根拠を確認',exact=True).click();page.get_by_role('heading',name='項目ごとの公開承認').wait_for()
   assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');result['checks'].append('393px detail no horizontal overflow')
   page.locator('textarea').first.fill('再開後も残る下書き');page.reload();page.get_by_role('button',name='記事・根拠を確認',exact=True).click();assert page.locator('textarea').first.input_value()=='再開後も残る下書き';result['checks'].append('reason draft retained after reload')
   # Approve both quoted articles, then identity. All proof URLs must be approved.
   for kind in ['article','article','event']:
    card=page.locator('article').filter(has=page.get_by_role('heading',name='記事： ／ unapproved' if kind=='article' else '事件： ／ unapproved',exact=True)).first
    card.get_by_role('button',name='承認前・仮承認後を比較',exact=True).click();page.locator('#previewDialog').wait_for(state='visible')
    preview=json.loads(page.locator('#previewContent').inner_text());assert preview['scope']=='hypothetical_not_approved';page.get_by_role('button',name='閉じる',exact=True).click()
    card.get_by_role('button',name='公開承認',exact=True).click();page.get_by_role('button',name='戻る',exact=True).click();assert page.locator('#confirmDialog').is_hidden();result['checks'].append(kind+' approval cancellation does not submit')
    card.get_by_role('button',name='公開承認',exact=True).click();page.locator('#confirmReason').fill('SYNTHETIC 原文と対象を確認');page.get_by_role('button',name='確認して実行',exact=True).dblclick();page.wait_for_function('()=>!busy')
    current=page.evaluate('state.approved_preview');assert current==preview['after'];result['checks'].append(kind+' hypothetical and approved projection match')
   page.get_by_role('button',name='公開プレビュー',exact=True).click();assert page.get_by_role('heading',name='承認済み出力のプレビュー').is_visible();result['checks'].append('approved preview shown')
   page.get_by_role('button',name='確認一覧',exact=True).click();page.get_by_role('button',name='記事・根拠を確認',exact=True).click()
   eventcard=page.locator('article').filter(has=page.get_by_role('heading',name='事件： ／ approved',exact=True))
   eventcard.get_by_role('button',name='公開取消し',exact=True).click();page.locator('#confirmReason').fill('SYNTHETIC 取消し確認');page.get_by_role('button',name='確認して実行',exact=True).click();page.wait_for_function('()=>!busy');assert page.evaluate('state.approved_preview.length')==0
   result['checks'].append('withdrawal removes incident from approved preview')
   page.get_by_role('button',name='操作履歴',exact=True).click();assert page.get_by_role('heading',name='承認・取消し履歴').is_visible()
   page.screenshot(path=str(base/'operator-mobile.png'),full_page=True)
   for width in (360,412):
    page.set_viewport_size({'width':width,'height':851});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');result['checks'].append(str(width)+'px history no horizontal overflow')
   result['page_errors']=errors;assert not errors;assert not result['outside_requests']
   # Host/origin/token checks, using localhost only.
   bad=context.request.post(address+'/api/operate',data='{}',headers={'Content-Type':'application/json','Origin':'https://evil.invalid'})
   assert bad.status==403;result['checks'].append('cross-origin missing-token POST blocked')
   bad=context.request.get(address+'/api/state',headers={'Host':'evil.invalid'});assert bad.status==403;result['checks'].append('foreign Host rejected')
   bad=context.request.post(address+'/api/operate',data='{}',headers={'Content-Type':'application/json','Origin':address,'X-Local-Token':'wrong'});assert bad.status==403;result['checks'].append('wrong local token rejected')
   page.get_by_role('button',name='確認一覧',exact=True).click();page.get_by_role('button',name='記事・根拠を確認',exact=True).click()
   eventcard=page.locator('article').filter(has=page.get_by_role('heading',name='事件： ／ withdrawn',exact=True));eventcard.get_by_role('button',name='再承認',exact=True).click();page.locator('#confirmReason').fill('SYNTHETIC 再承認');page.get_by_role('button',name='確認して実行',exact=True).click();page.wait_for_function('()=>!busy');assert page.evaluate('state.approved_preview.length')==1, (page.locator('#message').inner_text(),page.evaluate('state.items[0].targets.map(t=>[t.kind,t.key,t.status,t.warning])'));result['checks'].append('explicit reapproval restores isolated projection')
   with page.expect_download() as dl:page.get_by_role('button',name='確認記録をJSON保存',exact=True).click()
   dl.value.save_as(tmp+'/records.json');record=json.loads(Path(tmp+'/records.json').read_text());assert record['scope']=='isolated_review_only_not_authority';result['checks'].append('review-only JSON export')
   from importlib.util import spec_from_file_location,module_from_spec
   sys.path.insert(0,str(app.parent));import operator_preview
   offline_path=Path(tmp)/'offline.html';operator_preview.build(offline_path,demo=True)
   offline=browser.new_context(viewport={'width':393,'height':851},is_mobile=True,has_touch=True)
   outside=[]
   offline.route('**/*',lambda r:r.continue_() if r.request.url.startswith('file:') else (outside.append(r.request.url),r.abort()))
   opage=offline.new_page();opage.set_content(offline_path.read_text());opage.get_by_text('読み取り専用HTMLです。操作の保存',exact=False).wait_for();opage.get_by_role('button',name='記事・根拠を確認',exact=True).click();opage.get_by_role('button',name='公開承認',exact=True).first.click();assert opage.locator('#confirmDialog').is_hidden();assert not outside;result['checks'].append('offline HTML readable and cannot approve or fetch');offline.close()
   import operator_app as localapp
   db=localapp.open_owned(Path(tmp)/'operator.sqlite');article=localapp.events(db)[0]['articles'][-1].copy();article['title']+=' SYNTHETIC 見出し訂正';localapp.sync.change_source(db,'browser-correct-heading',article,localapp.sync.head(db,article['url'])['state_id'],localapp.now());db.close()
   page.reload();page.get_by_role('button',name='記事・根拠を確認',exact=True).click();assert page.get_by_role('heading',name='事件： ／ needs_recheck',exact=True).is_visible();assert page.evaluate('state.approved_preview.length')==0;result['checks'].append('changed article forces visible recheck and removes approved output')
   assert not errors;assert not result['outside_requests']

   browser.close()
 finally:
  server.terminate();server.wait(timeout=5)
(base/'browser_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
