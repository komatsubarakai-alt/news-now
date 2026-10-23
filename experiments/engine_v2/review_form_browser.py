"""Optional offline Chromium regression: use fictional HTML only, never operator results.

python review_form_browser.py --html /tmp/demo.html --output /tmp/results.json
Requires existing Playwright and Chromium; no automatic installation/networking.
"""
import argparse,json,tempfile
from pathlib import Path

def check(html_path,output,*,legacy=None):
    from playwright.sync_api import sync_playwright
    text=Path(html_path).read_text();requests=[];errors=[];results=[];legacy_result=None
    with sync_playwright() as p, tempfile.TemporaryDirectory(prefix='review-browser-') as temp:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-dev-shm-usage'])
        context=browser.new_context(is_mobile=True,has_touch=True,offline=True,accept_downloads=True)
        def route_request(route):
            if route.request.url.startswith('file:'):route.continue_()
            else:requests.append(route.request.url);route.abort()
        context.route('**/*',route_request)
        probe=context.new_page()
        try:
            probe.goto(Path(html_path).resolve().as_uri(),timeout=8000)
            assert probe.locator('#jsonTransfer').is_visible()
            local_file='desktop_file_scheme_tested'
        except Exception as error:
            if 'ERR_BLOCKED_BY_ADMINISTRATOR' not in str(error):raise
            local_file='unverified_managed_browser_policy_blocks_file_scheme'
        finally:probe.close()
        if legacy:
            page=context.new_page();page.set_content(Path(legacy).read_text())
            assert not page.locator('#jsonTransfer').is_visible()
            page.locator('#transfer > summary').click();assert page.locator('#jsonTransfer').is_visible()
            page.evaluate("()=>{window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async()=>{},close:async()=>{}})});}")
            page.locator('#exportJson').click();page.wait_for_function("()=>document.getElementById('notice').textContent.includes('書き込みが完了')")
            assert page.locator('#savedAck').is_disabled();legacy_result={'collapsed_import_reproduced':True,'ack_disabled_after_direct_save_reproduced':True};page.close()
        for width in (360,393,412):
            page=context.new_page();page.set_viewport_size({'width':width,'height':851});page.on('pageerror',lambda error:errors.append(str(error)));page.set_content(text)
            assert page.locator('#jsonTransfer').is_visible() and page.locator('#validateJson').is_visible() and page.locator('#chooseImportFile').is_visible()
            assert page.locator('#jsonTransfer').evaluate("el=>!el.closest('details')")
            assert page.locator('#savedAck').is_disabled();assert '読み込みのために押す必要はありません' in page.locator('#savedAckHelp').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # Direct save path must leave the explicit save acknowledgement usable.
            page.evaluate("()=>{window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async()=>{},close:async()=>{}})});}")
            page.locator('#exportJson').click();page.wait_for_function("()=>document.getElementById('notice').textContent.includes('書き込みが完了')")
            assert page.locator('#savedAck').is_enabled();page.locator('#savedAck').click();assert page.locator('#savedAck').is_enabled();assert '保存確認済み' in page.locator('#savedAck').inner_text()
            page.locator('.review-form').first.locator('select').first.select_option('same');page.locator('.review-form').first.locator('textarea').fill('架空の復元メモ <img src=x>');assert page.locator('#savedAck').is_disabled()
            # Download path + actual exported file, without opening a disclosure.
            page.evaluate('()=>{window.showSaveFilePicker=undefined;}')
            with page.expect_download() as info:page.locator('#exportJson').click()
            saved=Path(temp)/('saved-'+str(width)+'.json');info.value.save_as(saved);assert page.locator('#savedAck').is_enabled();page.locator('#savedAck').click();exported=saved.read_text();record_count=len(json.loads(exported)['records'])
            expected=page.evaluate('Array.from(reviewFormStore.targets.keys()).map(id=>reviewFormStore.current(id))')
            # Fresh local-document simulation: paste import before any export.
            page.set_content(text);page.locator('#jsonTransfer').fill(exported);page.locator('#validateJson').click();assert page.locator('#applyImport').is_visible();assert page.evaluate('reviewFormStore.history.length')==0
            page.locator('#applyImport').click();assert page.evaluate('Array.from(reviewFormStore.targets.keys()).map(id=>reviewFormStore.current(id))')==expected
            assert '復元完了' in page.locator('#restoreStatus').inner_text() and 'ファイルと一致 24対象' in page.locator('#restoreComparisonSummary').inner_text()
            assert page.locator('#restoreComparisonRows details').count()==24 and page.locator('img').count()==0
            assert page.locator('#savedAck').is_disabled()  # Import is not an export.
            # Repeat import, including older Android FileReader fallback.
            page.evaluate('()=>{File.prototype.text=undefined;}');page.locator('#importFile').set_input_files(saved);page.wait_for_function("()=>!document.getElementById('importPanel').hidden");assert saved.name in page.locator('#selectedFileName').inner_text();assert 'ファイル読み込み成功' in page.locator('#readStatus').inner_text();assert '検証成功' in page.locator('#verifyStatus').inner_text();assert '未実施' in page.locator('#restoreStatus').inner_text();page.locator('#applyImport').click();assert page.evaluate('reviewFormStore.history.length')==record_count
            assert saved.name in page.locator('#selectedFileName').inner_text() and page.locator('#importFile').input_value()==''
            assert '検証成功' in page.locator('#verifyStatus').inner_text() and '復元完了' in page.locator('#restoreStatus').inner_text()
            page.locator('#jsonTransfer').fill('{broken');page.locator('#validateJson').click();assert '拒否' in page.locator('#notice').inner_text();assert '検証失敗' in page.locator('#verifyStatus').inner_text();assert page.evaluate('reviewFormStore.history.length')==record_count
            page.locator('#importFile').set_input_files({'name':'broken.json','mimeType':'application/json','buffer':b'{broken'});page.wait_for_function("()=>document.getElementById('verifyStatus').textContent.includes('検証失敗')");assert '読み込み成功' in page.locator('#readStatus').inner_text() and '未実施' in page.locator('#restoreStatus').inner_text()
            # File read failure explicitly preserves state and guides paste.
            page.evaluate("()=>{File.prototype.text=()=>Promise.reject(Error('simulated content provider failure'));}")
            page.locator('#importFile').set_input_files(saved);page.wait_for_function("()=>document.getElementById('notice').textContent.includes('読み込み失敗')")
            assert '読み込み失敗' in page.locator('#readStatus').inner_text() and page.evaluate('reviewFormStore.history.length')==record_count
            # Missing filename still has independent success/validation/restore stages.
            page.evaluate("text=>{const input=document.getElementById('importFile');const file=new File([text],'');Object.defineProperty(file,'text',{value:()=>Promise.resolve(text)});Object.defineProperty(input,'files',{value:[file],configurable:true});input.dispatchEvent(new Event('change'));}",exported)
            page.wait_for_function("()=>!document.getElementById('importPanel').hidden")
            assert 'ファイル名を取得できません' in page.locator('#selectedFileName').inner_text()
            assert 'ファイル読み込み成功' in page.locator('#readStatus').inner_text();page.locator('#applyImport').click();assert '復元完了' in page.locator('#restoreStatus').inner_text()
            # Only the latest async selection may prepare an import.
            page.evaluate("text=>{window.completeSlow=null;const slow={name:'slow.json',size:text.length,text:()=>new Promise(resolve=>{window.completeSlow=()=>resolve(text)})};const fast={name:'fast.json',size:text.length,text:()=>Promise.resolve(text)};const input=document.getElementById('importFile');for(const file of [slow,fast]){Object.defineProperty(input,'files',{value:[file],configurable:true});input.dispatchEvent(new Event('change'));}}",exported)
            page.wait_for_function("()=>!document.getElementById('importPanel').hidden");page.evaluate('()=>{window.completeSlow();}');page.wait_for_timeout(100)
            assert page.locator('#selectedFileName').inner_text()=='fast.json';page.locator('#applyImport').click()
            # An older file must report preserved newer contents, not a false match.
            page.evaluate("()=>{const id=reviewFormStore.manifest.targets[0].target_id;const data=reviewFormStore.current(id);data.memo='新しい架空確認メモ';reviewFormStore.edit(id,data);reviewFormStore.exportJSON();}")
            page.locator('#jsonTransfer').fill(exported);page.locator('#validateJson').click();page.locator('#applyImport').click();assert '維持 1対象' in page.locator('#restoreComparisonSummary').inner_text()
            assert page.locator('.review-form').first.locator('textarea').input_value()=='新しい架空確認メモ'
            # A restore exception must not be displayed as success.
            page.locator('#jsonTransfer').fill(exported);page.locator('#validateJson').click()
            page.evaluate("()=>{window.originalApply=reviewFormStore.applyImport;reviewFormStore.applyImport=()=>{throw Error('simulated restore failure')};}")
            page.locator('#applyImport').click();assert '復元失敗' in page.locator('#restoreStatus').inner_text()
            page.evaluate('()=>{reviewFormStore.applyImport=window.originalApply;}')
            # Selection errors are distinguished from content validation errors.
            page.evaluate("()=>{const input=document.getElementById('importFile');window.originalChoose=input.click;input.click=()=>{throw Error('simulated chooser restriction')};}")
            page.locator('#chooseImportFile').click();assert 'ファイル選択失敗' in page.locator('#readStatus').inner_text()
            page.evaluate("()=>{document.getElementById('importFile').click=window.originalChoose;}")
            # Cancelled picker: no saved-success message; JSON remains accessible.
            page.evaluate("()=>{window.showSaveFilePicker=async()=>{throw new DOMException('cancel','AbortError')};}")
            page.locator('#exportJson').click();page.wait_for_function("()=>document.getElementById('notice').textContent.includes('取り消しました')")
            assert page.locator('#savedAck').is_enabled() and page.locator('#jsonTransfer').is_visible();page.locator('#savedAck').click()
            page.evaluate("document.querySelectorAll('details').forEach(x=>x.open=true)");assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            results.append({'width':width,'import_always_visible':True,'paste_before_export':True,'download_roundtrip':True,'file_reader_fallback':True,'failure_guidance':True,'ack_direct_and_download':True,'persistent_filename':True,'missing_filename_supported':True,'stages_separate':True,'restored_answers_and_memos_match':True,'out_of_order_read_ignored':True,'no_overflow':True})
            page.close()
        browser.close()
    assert not requests and not errors
    result={'checks':results,'legacy':legacy_result,'external_requests':len(requests),'page_errors':errors,'android_device':'unverified','content_scheme':'unverified','actual_file_navigation':local_file}
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--html',required=True);parser.add_argument('--output',required=True);parser.add_argument('--legacy-html');args=parser.parse_args();check(args.html,args.output,legacy=args.legacy_html)
