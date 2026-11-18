"""Offline regression/replay of an additive article/incident read-model."""
import argparse
import hashlib
import json
import socket
import sqlite3
import urllib.request
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from stage_compare import ROOT,evaluate
from engine import dumps
from compare import hashes
from operator_revalidation import validate_records,cohort
from article_store import (initialize,import_saved,import_reviews,import_quotes,quote_template,
                           compare_articles,digest)

def core_fingerprint(db):
    names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sep_')]
    return digest({name:db.execute('SELECT * FROM '+name).fetchall() for name in names})

def prepare(db,dataset_raw,review_raw):
    initialize(db);before=core_fingerprint(db)
    core={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sep_')}
    def authorize(action,table,*unused):
        if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and table in core:return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(authorize)
    try:
        import_saved(db);dataset,records=import_reviews(db,dataset_raw,review_raw)
        quoted=import_quotes(db,quote_template(dataset_raw,review_raw),dataset_raw,review_raw)
        pairs={}
        for row in dataset['rows']:
            if row['sample_id'] in ('real-022','real-030','real-033'):
                result=compare_articles(db,row['reference_article'],row['incoming_article'])
                result['event_links']=db.execute('SELECT event_id,role FROM sep_event_mentions WHERE version_id=? ORDER BY event_id,role',(result['left_version'],)).fetchall()
                pairs[row['sample_id']]=result
    finally:db.set_authorizer(None)
    if before!=core_fingerprint(db):raise AssertionError('article layer mutated earlier engine data')
    if db.execute('PRAGMA foreign_key_check').fetchall():raise AssertionError('broken foreign key')
    return {'core_tables_unchanged':True,'core_write_authorizer':'denied writes during article import',
        'table_counts':{name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sep_%'").fetchall()},
        'quote_status':quoted,'regression_pairs':pairs,
        'fetch_statuses':dict(db.execute('SELECT result,count(*) FROM sep_fetch_observations GROUP BY result'))}

GROUPS={
    'operator_same_both':('両本文確認・同一判断7組',{'operator_same_both'}),
    'operator_same_unconfirmed':('本文に未確認部分あり・同一判断6組',{'operator_same_partial','operator_same_neither'}),
    'operator_hold':('運営者保留2組',{'operator_hold_partial','operator_hold_neither'}),
    'provisional_same':('従来の暫定同一9組',{'provisional_same'}),
    'provisional_different':('従来の暫定別事件9組',{'provisional_different'}),
}

def run(root,dataset_raw,review_raw):
    before=hashes(root);dataset,records=validate_records(review_raw,dataset_raw)
    # Same corpus and replies; the new article layer never feeds dedup hints to
    # the identity validator and never removes a candidate or source quote.
    prior=evaluate(root,dataset,batch_size=(4,64));imports=[]
    def hook(db):imports.append(prepare(db,dataset_raw,review_raw))
    current=evaluate(root,dataset,batch_size=(4,64),db_hook=hook)
    saved=json.loads((root/'events.json').read_text());eligible=[e['event_id'] for e in saved if e.get('publication_status')!='held']
    source_rows={r['sample_id']:r for r in dataset['rows']};rows=[]
    for old,new in zip(prior['rows'],current['rows']):
        compare_fields=('sample_id','target_event_id','target_rank','target_retained','staged_total_bytes',
                        'staged_batches','staged_status','staged_wrong_merge','staged_changed_event_ids')
        if any(old[k]!=new[k] for k in compare_fields):raise AssertionError('separation changed identity behavior')
        source=source_rows[new['sample_id']];record=records.get(new['sample_id']);target=new['target_event_id']
        def metric(item,prefix):
            legacy=prefix=='legacy'
            return {'candidate_present':target in eligible if legacy else item['target_retained'],
                'position':eligible.index(target)+1 if legacy and target in eligible else None if legacy else item['target_rank'],
                'position_basis':'stored order, not relevance rank' if legacy else 'retrieval rank',
                'input_bytes':item[prefix+'_total_bytes'],'batches':item['legacy_calls' if legacy else 'staged_batches'],
                'status':item[prefix+'_status'],'cpu_ms':item[prefix+'_ms'],
                'wrong_merge_in_provisional_test':item[prefix+'_wrong_merge'],
                'changed_event_ids':item[prefix+'_changed_event_ids'],
                'input_preserved':item['legacy_input_preserved'] if legacy else item['input_preserved'],
                'candidate_catalog_preserved':item['legacy_catalog_preserved'] if legacy else item['all_candidate_states_preserved']}
        rows.append({'sample_id':new['sample_id'],'cohort':cohort(source,record),'event_id':target,
            'legacy':metric(new,'legacy'),'prior_prototype':metric(old,'staged'),'separated':metric(new,'staged'),
            'sqlite_bootstrap_with_article_import_ms':new['sqlite_bootstrap_ms']})
    summary={}
    for key,(label,cohorts) in GROUPS.items():
        selected=[r for r in rows if r['cohort'] in cohorts];summary[key]={'label':label,'samples':len(selected)}
        for method in ('legacy','prior_prototype','separated'):
            group=[r[method] for r in selected]
            summary[key][method]={'candidate_present':sum(r['candidate_present'] for r in group),
                'candidate_recall':sum(r['candidate_present'] for r in group)/len(group) if key in ('operator_same_both','provisional_same') else None,
                'positions':[r['position'] for r in group],'input_bytes':sum(r['input_bytes'] for r in group),
                'batches':sum(r['batches'] for r in group),'statuses':dict(Counter(r['status'] for r in group)),
                'pending':sum(r['status'] not in ('accepted','already_saved') for r in group),
                'provisional_wrong_merges':sum(bool(r['wrong_merge_in_provisional_test']) for r in group) if key.startswith('provisional_') else None,
                'cpu_ms':sum(r['cpu_ms'] for r in group)}
    if before!=hashes(root):raise AssertionError('input JSON mutated')
    if not all(r[m]['input_preserved'] and r[m]['candidate_catalog_preserved'] for r in rows for m in ('legacy','prior_prototype','separated')):
        raise AssertionError('input or candidate lost')
    if len(imports)!=len(rows) or not all(r['core_tables_unchanged'] for r in imports):raise AssertionError('missing separation import')
    return {'schema':'news-now-separation-comparison-v1','snapshot_revision':dataset['snapshot_revision'],
        'dataset_sha256':hashlib.sha256(dataset_raw).hexdigest(),'review_sha256':hashlib.sha256(review_raw).hexdigest(),
        'summary':summary,'rows':rows,'article_store':imports[0],
        'all_33_article_imports_preserved_core_tables':True,'prototype_before_after_outcomes_and_prompt_bytes_equal':True,
        'json_hashes_unchanged':True,'real_ai_api_calls':0,'actual_model_accuracy':'unconfirmed','actual_api_cost':'unconfirmed',
        'limitations':['article store is an additive import/read-model, not the new live canonical writer',
            'post-adoption/correction/undo synchronization and publication export are not implemented for sep tables',
            'legacy source bodies were not downloaded; quotation/check-time fields remain null/empty',
            'candidate recall is not AI decision accuracy; operator-reviewed seven pairs cover only five incidents',
            'same saved replies/label-driven mocks as earlier; operator decisions/quotes never inserted into prompts',
            'incoming URL withheld, but event names/summaries/reprints remain: no temporal holdout',
            'UTF-8 input sizes do not measure actual tokens or fees; replay does not charge budget reservations',
            'CPU includes only local work; article import is additional bootstrap work, not a speedup']}

def markdown(result):
    lines=['# 記事重複と事件同一性の分離：比較結果','',
        '旧＝現行エンジン、新＝最初4件→残りまとめ方式＋記事管理表。分離前のPR #17試作とも同条件で比較しました。',
        '本文確認済み7組・未確認部分あり6組・保留2組・従来の暫定18組を別集計。人の判断は正解ラベルやAI入力へ自動反映していません。',
        '','|区分|方式|対象保持|入力バイト|群数|保留|模擬再生の状態|','|---|---|---:|---:|---:|---:|---|']
    for group in result['summary'].values():
        for method,label in [('legacy','現行'),('prior_prototype','分離前試作'),('separated','分離後試作')]:
            row=group[method];lines.append(f"|{group['label']}|{label}|{row['candidate_present']}/{group['samples']}|{row['input_bytes']:,}|{row['batches']}|{row['pending']}|{row['statuses']}|")
    lines+=['','両本文確認7組では新旧とも7/7保持。新試作はすべて順位1位。現行の位置は保存順で、関連度順位ではありません。',
        '保留021の対象は既存のheld設定で自動候補外ですが、事件・元記事・判断記録は保持。候補にないことを新事件の根拠にしません。',
        '模擬不正回答による従来の暫定別事件9組への誤統合は新旧とも0。未確認部分のある組や保留を実際の正解率の分母にしません。',
        '','## 分離による変化','',
        '33組すべてで、分離前後の候補順位・保持・判断結果・変更先ID・入力バイトが一致しました。記事の分離だけによる入力削減や精度改善は未確認です。',
        '全体の保留は新旧とも22/33組。本文確認済み7組でも採用相当1、保留6のままです。引用を捏造して保留を減らしていません。',
        '読み込み時に記事管理表を追加するため、準備時間・保存量は増えます。速度改善を主張しません。',
        '','## 実記事の回帰確認','',
        '022：同じ保存記事版に、厚岸と札幌北区の2事件IDが対応したまま残る。記事重複判定で事件を書き換える処理はありません。',
        '030：見出し・日時一致でも別URL/別記事版を保持。転載関係は未確認候補。',
        '033：近似見出しでも媒体・公開日時・表記差を保持。完全一致や転載確定にはしない。',
        '','## 引用・取得状態','',
        '本文確認済み7組の引用テンプレートは14出典。URLと既存判断の記録日時だけを引き継ぎ、本文の確認日時は全14出典でnull、引用は空配列です。',
        '判断記録日時と本文確認日時を区別し、本文確認日時を推定しません。引用を今後記入しても原文未検証・正解未昇格の記録として追記します。',
        '実データにあるのは削除申告5記事と本文表示なし申告1記事。HTTPで実測した取得失敗・削除・空本文・取得成功は0で、区別の検証には模擬記録を使いました。',
        '','## 本番移行の阻止条件','',
        '1. 新管理表は読み込み専用の試作であり、新規採用・訂正・取消しとの同一トランザクション同期と公開出力は未実装。',
        '2. 1記事のどの引用がどの事件に対応するかの確定、転載の確認・取消し、記事改訂の順序判定が未完成。',
        '3. 本文引用と実際の確認日時が未提供。実AI精度・実API料金、独立した難しい続報の十分な標本も未確認。',
        '4. 元のID/記事/判断/履歴を維持した移行・復旧・予算下の継続試験を通すまで本番へ切り替えない。',
        '','次は7組の短い原文引用・確認日時を記入して検証し、事件ごとの引用範囲を定義します。記事の重複を理由に根拠・候補・事件との対応を捨てる最適化は行いません。']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,default=ROOT);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    root=a.snapshot.resolve();out=a.output_dir.resolve()
    if out.is_relative_to(root):p.error('output must be outside input snapshot')
    here=Path(__file__).parent;dataset_raw=(here/'evaluation.json').read_bytes();review_raw=(here/'operator_review_records.json').read_bytes();attempts=[]
    def deny(*args,**kw):attempts.append(True);raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=run(root,dataset_raw,review_raw)
    if attempts:raise AssertionError('network attempted')
    result['network_attempts']=0;out.mkdir(parents=True,exist_ok=True)
    (out/'separation_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'SEPARATION_RESULTS.md').write_text(markdown(result))
    (out/'source_quote_template.json').write_text(json.dumps(quote_template(dataset_raw,review_raw),ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'summary':result['summary'],'article_store':result['article_store'],'json_hashes_unchanged':True},ensure_ascii=False))

if __name__=='__main__':main()
