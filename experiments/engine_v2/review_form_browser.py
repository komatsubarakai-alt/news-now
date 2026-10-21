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
            assert page.locator('#jsonTransfer').is_visible() and page.locator('#validateJson').is_visible() and page.locator('#importFile').is_visible()
            assert page.locator('#jsonTransfer').evaluate("el=>!el.closest('details')")
            assert page.locator('#savedAck').is_disabled();assert '読み込みのために押す必要はありません' in page.locator('#savedAckHelp').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # Direct save path must leave the explicit save acknowledgement usable.
            page.evaluate("()=>{window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async()=>{},close:async()=>{}})});}")
            page.locator('#exportJson').click();page.wait_for_function("()=>document.getElementById('notice').textContent.includes('書き込みが完了')")
            assert page.locator('#savedAck').is_enabled();page.locator('#savedAck').click();assert page.locator('#savedAck').is_enabled();assert '保存確認済み' in page.locator('#savedAck').inner_text()
            page.locator('.review-form').first.locator('select').first.select_option('same');assert page.locator('#savedAck').is_disabled()
            # Download path + actual exported file, without opening a disclosure.
            page.evaluate('()=>{window.showSaveFilePicker=undefined;}')
            with page.expect_download() as info:page.locator('#exportJson').click()
            saved=Path(temp)/(str(width)+'.json');info.value.save_as(saved);assert page.locator('#savedAck').is_enabled();page.locator('#savedAck').click();exported=saved.read_text();record_count=len(json.loads(exported)['records'])
            expected=page.evaluate('Array.from(reviewFormStore.targets.keys()).map(id=>reviewFormStore.current(id))')
            # Fresh local-document simulation: paste import before any export.
            page.set_content(text);page.locator('#jsonTransfer').fill(exported);page.locator('#validateJson').click();assert page.locator('#applyImport').is_visible();assert page.evaluate('reviewFormStore.history.length')==0
            page.locator('#applyImport').click();assert page.evaluate('Array.from(reviewFormStore.targets.keys()).map(id=>reviewFormStore.current(id))')==expected
            assert page.locator('#savedAck').is_disabled()  # Import is not an export.
            # Repeat import, including older Android FileReader fallback.
            page.evaluate('()=>{File.prototype.text=undefined;}');page.locator('#importFile').set_input_files(saved);page.wait_for_function("()=>!document.getElementById('importPanel').hidden");page.locator('#applyImport').click();assert page.evaluate('reviewFormStore.history.length')==record_count
            page.locator('#jsonTransfer').fill('{broken');page.locator('#validateJson').click();assert '拒否' in page.locator('#notice').inner_text();assert page.evaluate('reviewFormStore.history.length')==record_count
            # File read failure explicitly preserves state and guides paste.
            page.evaluate("()=>{File.prototype.text=()=>Promise.reject(Error('simulated content provider failure'));}")
            page.locator('#importFile').set_input_files(saved);page.wait_for_function("()=>document.getElementById('notice').textContent.includes('読み込み失敗')")
            assert '貼り付け' in page.locator('#notice').inner_text() and page.evaluate('reviewFormStore.history.length')==record_count
            # Cancelled picker: no saved-success message; JSON remains accessible.
            page.evaluate("()=>{window.showSaveFilePicker=async()=>{throw new DOMException('cancel','AbortError')};}")
            page.locator('#exportJson').click();page.wait_for_function("()=>document.getElementById('notice').textContent.includes('取り消しました')")
            assert page.locator('#savedAck').is_enabled() and page.locator('#jsonTransfer').is_visible();page.locator('#savedAck').click()
            page.evaluate("document.querySelectorAll('details').forEach(x=>x.open=true)");assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            results.append({'width':width,'import_always_visible':True,'paste_before_export':True,'download_roundtrip':True,'file_reader_fallback':True,'failure_guidance':True,'ack_direct_and_download':True,'no_overflow':True})
            page.close()
        browser.close()
    assert not requests and not errors
    result={'checks':results,'legacy':legacy_result,'external_requests':len(requests),'page_errors':errors,'android_device':'unverified','content_scheme':'unverified','actual_file_navigation':local_file}
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--html',required=True);parser.add_argument('--output',required=True);parser.add_argument('--legacy-html');args=parser.parse_args();check(args.html,args.output,legacy=args.legacy_html)
