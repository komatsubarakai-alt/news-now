"""Fictional read-only phone display checks. No external requests or real news."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
import operator_app as app
from operator_task_demo import populate
import quote_bindings


def run(output):
    output = Path(output).resolve()
    if output.is_relative_to(app.ROOT): raise ValueError('output must be outside repository')
    output.mkdir(parents=True, exist_ok=True)
    db = app.connect(); app.initialize(db); populate(db)
    initial = app.snapshot(db)
    task = next(t for t in initial['work']['tasks'] if t.get('article', {}).get('url', '').endswith('demo-08'))
    eid = task['suggested_event_id']
    quote_bindings.record(db, 'fictional-old-quote', event_id=eid, url=task['article']['url'],
        quote='【架空】当時の記事版が不明な引用', kind='unknown', origin='saved_ai_response', now=datetime.now(timezone.utc))
    state = app.snapshot(db); db.close()
    data = json.dumps(state, ensure_ascii=False).replace('<', '\\u003c')
    html = Path(app.__file__).with_name('operator_ui.html.template').read_text()
    html = html.replace('<script id="offlineSnapshot" type="application/json">null</script>',
                        '<script id="offlineSnapshot" type="application/json">' + data + '</script>').replace('__LOCAL_TOKEN__', 'offline-read-only')
    html = html.replace('<title>', '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; base-uri \'none\'; form-action \'none\'"><title>', 1)
    checks = []; requests = []; errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(is_mobile=True, has_touch=True)
        context.route('**/*', lambda r: (requests.append(r.request.url), r.abort()))
        page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content(html); page.wait_for_function('()=>state!==null')
        for width in (360, 393, 412):
            page.set_viewport_size({'width': width, 'height': 851})
            page.evaluate("()=>openTask(state.work.tasks.find(t=>t.article?.url.endsWith('demo-08')))")
            note = page.locator('.quote-binding-note').first
            note.evaluate("node=>{for(let p=node.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;}")
            assert note.inner_text().find('紐付け未確認') >= 0, note.inner_text()
            assert note.locator('input[type=checkbox]').count() == 0
            page.locator('.quote-input > summary').first.click()
            page.get_by_label('追加する引用の原文', exact=True).first.fill('【架空】見出しの引用')
            assert page.get_by_role('button', name='閲覧専用：引用は保存できません', exact=True).first.is_disabled()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            checks.append(str(width) + 'px: unbound quotation, no selectable proof, disabled save, no overflow')
        assert not requests and not errors
        checks += ['external requests zero', 'JS errors zero']
        browser.close()
    (output / 'browser-results.json').write_text(json.dumps({'checks': checks, 'actual_android': 'unverified'}, ensure_ascii=False, indent=2))
    print(json.dumps({'checks': len(checks), 'network_requests': 0, 'android': 'unverified'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output-dir', required=True)
    run(parser.parse_args().output_dir)
