"""Generate an OFFLINE READ-ONLY phone HTML; approvals require the loopback app."""
import argparse
import json
import tempfile
from pathlib import Path
import operator_app as app
from operator_demo import populate


def build(output, snapshot_root=None, demo=False):
    output=Path(output)
    if output.is_symlink() or output.resolve().is_relative_to(app.ROOT): raise ValueError('HTML must be outside repository')
    if snapshot_root and output.resolve().is_relative_to(Path(snapshot_root).resolve()): raise ValueError('HTML must be outside input snapshot')
    db=app.connect();app.initialize(db)
    try:
        if demo:populate(db)
        elif snapshot_root:
            app.import_snapshot(db,Path(snapshot_root))
            for event in app.events(db):
                for article in event.get('articles',[]):
                    if not db.execute('SELECT 1 FROM sync_heads WHERE url=?',(article['url'],)).fetchone():app.sync.seed(db,article)
        else:raise ValueError('explicit demo or snapshot required')
        state=app.snapshot(db)
        encoded=json.dumps(state,ensure_ascii=False).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
        html=Path(app.__file__).with_name('operator_ui.html.template').read_text()
        html=html.replace('<script id="offlineSnapshot" type="application/json">null</script>',
                          '<script id="offlineSnapshot" type="application/json">'+encoded+'</script>')
        html=html.replace('__LOCAL_TOKEN__','offline-read-only').replace('<title>',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; base-uri \'none\'; form-action \'none\'"><title>',1)
        output.write_text(html,encoding='utf-8');return {'scope':'read_only_local_html','can_publish':False,'events':len(state['items'])}
    finally:db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);g=p.add_mutually_exclusive_group(required=True);g.add_argument('--snapshot');g.add_argument('--demo',action='store_true');a=p.parse_args();print(build(a.output,a.snapshot,a.demo))
