"""Generate isolated review forms; no production writer or network client."""
import argparse,copy,hashlib,json,re,html
from collections import defaultdict
from pathlib import Path
import operator_applicability as a
HERE=Path(__file__).resolve().parent

def reference(article):
    ref=a.article_ref(article)
    return {**ref,'article_version_id':ref['stored_version_id'] or 'derived:'+ref['derived_version_sha256'],
            'version_origin':'stored' if ref['stored_version_id'] else 'derived_saved_metadata',
            'full_saved_sha256':a.digest(article)}

def manifest(result):
    grouped=defaultdict(list);ids=set()
    for row in result['assessments']:
        d=row['original_decision'];did=d['decision_id']
        if did in ids:raise ValueError('duplicate judgment ID')
        ids.add(did)
        if a.digest({k:d.get(k) for k in a.DECISION_FIELDS})!=did:raise ValueError('invalid original judgment ID')
        if a.target_hash(row['original_target'])!=row['saved_hash'] or d['target_hash']!=row['saved_hash']:raise ValueError('old hash mismatch')
        if a.target_hash(row['current_target'])!=row['current_hash']:raise ValueError('current hash mismatch')
        if row['event_id']!=d['event_id'] or row['current_target']['event_id']!=d['event_id']:raise ValueError('event ID mismatch')
        if a.digest(row['source_group'])!=d['group_hash']:raise ValueError('source version mismatch')
        grouped[row['event_id']].append(row)
    targets=[]
    for eid,rows in sorted(grouped.items()):
        if len({(r['saved_hash'],r['current_hash']) for r in rows})!=1:raise ValueError('multiple snapshots for one event')
        r=rows[0]
        versions={'target_before':[reference(v) for v in r['original_target']['articles']],
                  'target_current':[reference(v) for v in r['current_target']['articles']]}
        common={'event_id':eid,'judgment_hash':r['saved_hash'],'current_hash':r['current_hash']}
        targets.append({**common,'target_id':'event:'+eid,'scope':'event','judgment_id':None,
                        'judgment_ids':sorted(x['decision_id'] for x in rows),'original_record_hashes':sorted(a.digest(x['original_decision']) for x in rows),
                        'article_versions':{**versions,'judgment_sources':sorted([{'judgment_id':x['decision_id'],'articles':[reference(v) for v in x['source_group']['articles']]} for x in rows],key=lambda x:x['judgment_id'])}})
        for r in sorted(rows,key=lambda r:r['decision_id']):
            targets.append({**common,'target_id':'judgment:'+r['decision_id'],'scope':'judgment','judgment_id':r['decision_id'],
                            'judgment_ids':[r['decision_id']],'original_record_hashes':[a.digest(r['original_decision'])],
                            'article_versions':{**versions,'judgment_sources':[{'judgment_id':r['decision_id'],'articles':[reference(v) for v in r['source_group']['articles']]}]}})
    return {'format_version':1,'dataset_id':a.digest(targets),'targets':targets}

def render(result):
    result=copy.deepcopy(result);m=manifest(result);page=a.render(result)
    # Match existing static layout without changing its read-only generator.
    grouped=defaultdict(list)
    for r in result['assessments']:grouped[(r['event_id'],r['saved_hash'],r['current_hash'])].append(r)
    ordered=sorted(grouped.values(),key=lambda rs:(all(r['state']=='candidate' for r in rs),rs[0]['event_id']))
    for i,rows in enumerate(ordered):
        page=page.replace('<section id="event-'+str(i)+'">','<section id="event-'+str(i)+'" data-review-event="'+html.escape(rows[0]['event_id'],quote=True)+'">')
        for r in rows:
            marker='<details class="judgment"><summary>'+a.STATES[r['state']]+' ／ 判断 '+r['decision_id'][:12]
            replacement='<details class="judgment" data-review-judgment="'+r['decision_id']+'"><summary>'+a.STATES[r['state']]+' ／ 判断 '+r['decision_id'][:12]
            if marker not in page:raise ValueError('judgment layout not matched')
            page=page.replace(marker,replacement,1)
    page=page.replace("default-src 'none'; style-src", "default-src 'none'; script-src 'unsafe-inline'; connect-src 'none'; style-src")
    page=page.replace('運営者判断の差分確認（閲覧専用）','運営者確認結果の記録（隔離）')
    page=page.replace('<h1>運営者判断の差分確認</h1>','<h1>今回の確認結果を記録</h1>')
    page=page.replace('<strong>閲覧専用・非公開ローカル用</strong>：判断変更・承認・保存・本番反映はできません。','<strong>非公開・隔離された確認記録</strong>：元の判断・承認・本番データは変更しません。入力はJSONへ出力するまで端末のファイルに保存されません。')
    page=page.replace('</style>','label{display:block;margin:10px 0}select,textarea,button{font:inherit;width:100%;min-height:48px;padding:9px;margin:5px 0}textarea{min-height:90px}button{cursor:pointer;border:1px solid #666;border-radius:8px;background:#edf3fa}.review-form{border:2px solid #49617d;border-radius:8px;padding:10px;margin:12px 0}.record-status{font-weight:bold}.tools{border:2px solid #555;border-radius:8px;padding:12px;margin:12px 0}#notice{white-space:pre-wrap}#jsonTransfer{min-height:170px}#importPanel[hidden]{display:none}fieldset{min-width:0;border:0;padding:0}input{font:inherit;max-width:100%;min-height:48px}.record-heading{font-size:18px}body{padding-bottom:40px}html{scroll-padding:12px}</style>')
    toolbar='''<div class="tools"><p><strong>記録 ≠ 承認・本番反映</strong>。事件全体の回答は個別判断へコピーされません。JSONは非公開資料として保存してください。</p><p>記事版IDの「derived:」は保存内容から作った照合用IDです。過去に保存された正式版IDや、本文・出典の確認済みを意味しません。</p><button id="exportJson" type="button">確認結果をJSONへ書き出す</button><button id="savedAck" type="button" disabled>JSONを保存したことを確認</button><details id="transfer"><summary>JSONのコピー・読み込み</summary><p>ダウンロードできない場合は、下のJSONをコピーして非公開のテキストファイルに保存してください。Dropboxへ自動送信しません。</p><textarea id="jsonTransfer" aria-label="JSONの出力・貼り付け" spellcheck="false"></textarea><button id="copyJson" type="button">出力JSONをコピー</button><label>確認結果JSONを選ぶ<input id="importFile" type="file" accept=".json,application/json"></label><button id="validateJson" type="button">貼り付けたJSONを検証</button></details><div id="importPanel" hidden><p id="importSummary"></p><button id="applyImport" type="button">検証済みの確認結果を取り込む</button><button id="cancelImport" type="button">取り込みをやめる</button></div><p id="notice" role="status" aria-live="polite">入力待ち。未入力は未確認のままです。再読み込み前にJSONを出力してください。</p></div><noscript>JavaScriptが無効なため入力とJSON出力・復元はできません。元資料の閲覧のみ可能です。</noscript>'''
    page=page.replace('<nav>',toolbar+'<nav>',1)
    data=a.canonical(m).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    code=(HERE/'review_form_core.js').read_text();ui=(HERE/'review_form_ui.js').read_text()
    if '</script' in code.lower() or '</script' in ui.lower():raise ValueError('unsafe inline code')
    return page.replace('</body>',f'<script id="reviewManifest" type="application/json">{data}</script><script>{code}\n{ui}</script></body>')

def write(result,path):
    path=Path(path)
    if path.is_symlink() or path.resolve().is_relative_to(a.ROOT):raise ValueError('generate private HTML outside repository')
    path.write_text(render(result),encoding='utf-8')

if __name__=='__main__':
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--input');g.add_argument('--demo',action='store_true');p.add_argument('--output',required=True);args=p.parse_args()
    if args.demo:
        from operator_applicability_demo import examples
        all_rows=a.evaluate(examples());rows=[r for r in all_rows['assessments'] if r['state']=='recheck']
    else:rows=json.loads(Path(args.input).read_text())['assessments']
    result={'assessments':rows,'counts':{k:sum(r['state']==k for r in rows) for k in a.STATES}};write(result,args.output)
    print(json.dumps({'events':len({r['event_id'] for r in rows}),'judgments':len(rows),'production_connected':False}))
