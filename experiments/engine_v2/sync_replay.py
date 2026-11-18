"""Read-only saved-news compatibility replay plus separate synthetic mutations."""
import argparse
import copy
import json
import socket
import sys
import tempfile
import urllib.request
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from stage_compare import ROOT,evaluate
from separation_compare import prepare as prepare_articles
from operator_revalidation import validate_records,cohort
from engine import connect,import_snapshot,events,atomic,dumps
from article_store import import_saved,version_id
from event_tracker import analysis_prompt,TRACKING_RESPONSE
from compare import hashes
from revision_sync import (initialize,seed,head,add_evidence,record_reference,change_source,
    verify_quote,make_job,call_mock,adopt,cancel,bind_operator_reviews,digest,refresh_links)

NOW=datetime(2026,10,15,12,tzinfo=timezone.utc) # Explicit simulation operation time.

def legacy_fingerprint(db):
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith(('sep_','sync_'))]
    return digest({name:db.execute('SELECT * FROM '+name).fetchall() for name in tables})

def saved_operator_probe(root,dataset_raw,review_raw):
    db=connect();import_snapshot(db,root);import_saved(db);initialize(db);before=legacy_fingerprint(db)
    binding=bind_operator_reviews(db,dataset_raw,review_raw);dataset,_=validate_records(review_raw,dataset_raw)
    sample=next(r for r in dataset['rows'] if r['sample_id']=='real-022');article=copy.deepcopy(sample['incoming_article']);article['title']+='【模擬見出し訂正】'
    result=change_source(db,'operator-probe-title',article,head(db,article['url'])['state_id'],NOW)
    expected={binding['bound']['real-022']}
    if set(result['affected_decisions'])!=expected or before!=legacy_fingerprint(db):raise AssertionError('operator impact or legacy mutation')
    output={'basis':'provided review records, explicitly simulated metadata change',
        'bound_records':len(binding['bound']),'unbound':binding['unbound_manual_selection'],
        'changed_sample':'real-022','affected_sample_ids':['real-022'],
        'expected_impact_exact':True,'other_operator_records_still_current':sum(
            db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(did,)).fetchone()[0]=='current'
            for sid,did in binding['bound'].items() if sid!='real-022'),
        'legacy_tables_unchanged':True,'original_operator_records_preserved':db.execute('SELECT count(*) FROM sep_review_records').fetchone()[0]}
    db.close();return output

def shared_source_probe(root,dataset_raw):
    dataset=json.loads(dataset_raw);sample=next(r for r in dataset['rows'] if r['sample_id']=='real-022')
    shared=copy.deepcopy(sample['incoming_article']);incident_ids=sample['stored_reference_event_ids']
    db=connect();import_snapshot(db,root);import_saved(db);initialize(db);before=legacy_fingerprint(db)
    original='【模擬本文】事故Aの金額は100万円。事故Bの情報。'
    revised='【模擬本文】事故Aの金額は120万円。事故Bの情報。'
    seed(db,shared,body_text=original);decisions=[]
    for index,eid in enumerate(incident_ids):
        ev=add_evidence(db,eid,shared['url'],'body','100万円')
        decisions.append(record_reference(db,'shared-ai-'+str(index),eid,'ai','rejected',{'basis':'synthetic_reference'},[ev],NOW)['decision_id'])
    ev=add_evidence(db,incident_ids[0],shared['url'],'body','100万円')
    operator=record_reference(db,'shared-operator',incident_ids[0],'operator','same_event',{'basis':'synthetic_operator_reference'},[ev],NOW)['decision_id']
    decisions.append(operator)
    unrelated=next(e for e in events(db) if e['event_id'] not in incident_ids and len(e.get('articles',[]))==1)
    article=unrelated['articles'][0];seed(db,article,body_text='【模擬本文】無関係な案件。')
    other_ev=add_evidence(db,unrelated['event_id'],article['url'],'body','無関係')
    other=record_reference(db,'unrelated',unrelated['event_id'],'ai','rejected',{'basis':'synthetic_reference'},[other_ev],NOW)['decision_id']
    group={'event_name':article['title'],'category':unrelated.get('category','事件・事故'),'tracking_value':'high','articles':[article]}
    jid=make_job(db,unrelated['event_id'],group,[other_ev],NOW)
    cached=call_mock(db,jid,lambda prompt:copy.deepcopy(TRACKING_RESPONSE),NOW)
    if cached['status']!='cached':raise AssertionError('saved simulation budget insufficient; no limits may be reset')
    cache_before=db.execute('SELECT * FROM sync_answer_cache').fetchall()
    before_head=head(db,shared['url']);changed=change_source(db,'shared-body',shared,before_head['state_id'],NOW,body_text=revised)
    if set(changed['affected_decisions'])!=set(decisions) or set(changed['affected_events'])!=set(incident_ids):raise AssertionError('shared source fanout incorrect')
    if db.execute('SELECT * FROM sync_answer_cache').fetchall()!=cache_before:raise AssertionError('unrelated raw cache changed')
    unrelated_status=db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(other,)).fetchone()[0]
    if unrelated_status!='current' or call_mock(db,jid,lambda p:(_ for _ in ()).throw(AssertionError('cache')),NOW)['status']!='cached':raise AssertionError('unrelated judgment invalidated')
    checks={'historical':verify_quote(db,ev),'current_old_reference':verify_quote(db,ev,current=True),
        'new_body_old_quote':verify_quote(db,add_evidence(db,incident_ids[0],shared['url'],'body','100万円')),
        'new_body_new_quote':verify_quote(db,add_evidence(db,incident_ids[0],shared['url'],'body','120万円'))}
    title_ev=add_evidence(db,incident_ids[0],shared['url'],'title')
    title_decision=record_reference(db,'title-ref',incident_ids[0],'ai','rejected',{'basis':'synthetic_reference'},[title_ev],NOW)['decision_id']
    heading=copy.deepcopy(shared);heading['title']+='【模擬見出し更新】'
    title_result=change_source(db,'shared-title',heading,head(db,shared['url'])['state_id'],NOW,body_text=revised)
    if title_result['affected_decisions']!=[title_decision]:raise AssertionError('title-only impact not selective')
    date_ev=add_evidence(db,incident_ids[0],shared['url'],'published')
    date_decision=record_reference(db,'date-ref',incident_ids[0],'ai','rejected',{'basis':'synthetic_reference'},[date_ev],NOW)['decision_id']
    dated=copy.deepcopy(heading);dated['published']='2026-10-16T00:00:00Z'
    date_result=change_source(db,'shared-date',dated,head(db,shared['url'])['state_id'],NOW,body_text=revised)
    if date_result['affected_decisions']!=[date_decision]:raise AssertionError('date-only impact not selective')
    timeout=change_source(db,'shared-timeout',dated,head(db,shared['url'])['state_id'],NOW,outcome='request_failed',error='synthetic timeout')
    deleted=change_source(db,'shared-deleted',dated,head(db,shared['url'])['state_id'],NOW,outcome='deleted')
    if timeout['affected_decisions'] or len(deleted['affected_decisions'])!=5:raise AssertionError('availability semantics incorrect')
    if before!=legacy_fingerprint_except_simulation_budget(db):
        raise AssertionError('probe modified legacy data outside copied simulation budget')
    output={'basis':'real-022 URL and incident IDs, synthetic bodies/changes/references',
        'shared_incident_ids':incident_ids,'body_changed_affected_decisions':len(changed['affected_decisions']),
        'body_changed_affected_events':len(changed['affected_events']),'unrelated_decision_current':unrelated_status=='current',
        'unrelated_answer_cache_exactly_preserved':db.execute('SELECT * FROM sync_answer_cache').fetchall()==cache_before,
        'legacy_tables_except_simulation_budget_unchanged':True,
        'title_only_affected_decisions':len(title_result['affected_decisions']),
        'publication_only_affected_decisions':len(date_result['affected_decisions']),
        'timeout_affected_decisions':len(timeout['affected_decisions']),
        'deleted_affected_decisions':len(deleted['affected_decisions']),
        'quote_checks_synthetic_only':checks,'real_article_body_or_accuracy_claim':False}
    db.close();return output

def legacy_fingerprint_except_simulation_budget(db):
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith(('sep_','sync_'))]
    values={name:db.execute('SELECT * FROM '+name+(' WHERE name!=\'simulation\'' if name=='budget' else '')).fetchall() for name in tables}
    return digest(values)

def stored_revision_probe(root):
    db=connect();import_snapshot(db,root);import_saved(db);initialize(db)
    variants={}
    for url,payload in db.execute('SELECT url,payload FROM sep_versions ORDER BY version_id'):
        article=json.loads(payload);variants.setdefault(url,[]).append(article)
    url,articles=next((url,items) for url,items in variants.items() if len(items)>1 and any(
        a.get('published')!=items[0].get('published') for a in items[1:]) and any(any(v['url']==url for v in e.get('articles',[])) for e in events(db)))
    left=articles[0];right=next(a for a in articles[1:] if a.get('published')!=left.get('published'))
    event_id=next(e['event_id'] for e in events(db) if any(a['url']==url for a in e.get('articles',[])))
    seed(db,left);proof=add_evidence(db,event_id,url,'published')
    decision=record_reference(db,'saved-date-ref',event_id,'ai','rejected',{'basis':'saved_metadata_reference_only'},[proof],NOW)['decision_id']
    updated=change_source(db,'saved-variant-replay',right,head(db,url)['state_id'],NOW)
    if updated['affected_decisions']!=[decision]:raise AssertionError('saved version dependency mismatch')
    result={'url':url,'event_id':event_id,'old_version':version_id(left),'new_version':version_id(right),
        'old_published':left.get('published'),'new_published':right.get('published'),
        'affected_decisions':1,'basis':'two actually saved variants; replay order is synthetic, not publication chronology',
        'historical_version_retained':db.execute('SELECT count(*) FROM sync_source_states').fetchone()[0]==2}
    db.close();return result

def recovery_probe():
    sys.path.insert(0,str(ROOT/'tests'))
    from test_tracking import event,article,report
    with tempfile.TemporaryDirectory(prefix='news-now-sync-recovery-') as temporary:
        root=Path(temporary);e=event();incoming=article('容疑者を起訴','https://example.test/sync-followup',NOW.isoformat())
        group={'event_name':'江別豊幌住宅強盗','category':'事件・事故','tracking_value':'high','articles':[incoming]}
        (root/'events.json').write_text(json.dumps([e],ensure_ascii=False));(root/'tracking_pending.json').write_text(json.dumps([group],ensure_ascii=False))
        path=root/'shadow.sqlite';db=connect(str(path));import_snapshot(db,root);initialize(db)
        seed(db,e['articles'][0]);seed(db,incoming,body_text='【模擬本文】金額100万円。')
        proof=add_evidence(db,'e1',incoming['url'],'body','100万円');jid=make_job(db,'e1',group,[proof],NOW)
        new_prompt=db.execute('SELECT prompt FROM sync_jobs WHERE job_id=?',(jid,)).fetchone()[0]
        old_bytes=len(analysis_prompt([e],[incoming],'',group['event_name']).encode());calls=[]
        answer=copy.deepcopy(TRACKING_RESPONSE);answer.update(report(incoming))
        def fail_once(call):
            try:call()
            except RuntimeError:return
            raise AssertionError('expected injected failure')
        fail_once(lambda:call_mock(db,jid,lambda p:answer,NOW,failpoint='after_reserve'))
        budget=db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0]
        fail_once(lambda:call_mock(db,jid,lambda p:(calls.append(1) or answer),NOW,failpoint='after_cache'))
        db.close();db=connect(str(path));call_mock(db,jid,lambda p:(_ for _ in ()).throw(AssertionError('duplicate mock call')),NOW)
        before=events(db);fail_once(lambda:adopt(db,'first',jid,NOW,failpoint='after_guard'))
        if events(db)!=before:raise AssertionError('failed adoption changed event')
        fail_once(lambda:adopt(db,'first',jid,NOW,failpoint='after_commit'));db.close();db=connect(str(path))
        first=adopt(db,'first',jid,NOW)
        if db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0]!=budget:raise AssertionError('retry charged twice')
        updated=copy.deepcopy(incoming);updated['description']='【模擬訂正】120万円。'
        change_source(db,'correct-body-and-description',updated,head(db,incoming['url'])['state_id'],NOW,body_text='【模擬本文】金額120万円。')
        corrected_group=copy.deepcopy(group);corrected_group['articles']=[updated]
        fresh_proof=add_evidence(db,'e1',incoming['url'],'body','120万円');job2=make_job(db,'e1',corrected_group,[fresh_proof],NOW)
        answer2=copy.deepcopy(TRACKING_RESPONSE);answer2.update(report(updated,meaningful_change=False))
        call_mock(db,job2,lambda p:(calls.append(1) or answer2),NOW);second=adopt(db,'second',job2,NOW,supersedes=first['decision_id'])
        accepted=events(db);fail_once(lambda:cancel(db,'cancel-second','second',NOW,failpoint='after_undo'))
        if events(db)!=accepted:raise AssertionError('failed cancellation changed event')
        fail_once(lambda:cancel(db,'cancel-second','second',NOW,failpoint='after_commit'));cancel(db,'cancel-second','second',NOW)
        budget2=db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0]
        job3=make_job(db,'e1',corrected_group,[fresh_proof],NOW);call_mock(db,job3,lambda p:(_ for _ in ()).throw(AssertionError('readopt cache')),NOW)
        third=adopt(db,'third',job3,NOW)
        if third['status']!='accepted' or second['status']!='accepted' or len(calls)!=2:raise AssertionError('guarded lifecycle failed')
        if db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0]!=budget2:raise AssertionError('readopt charged twice')
        result={'basis':'synthetic fixture only, not real accuracy data','mock_calls':len(calls),
            'distinct_reservations':db.execute('SELECT count(*) FROM sync_reservations').fetchone()[0],
            'extra_reservations_for_retries_and_readoption':0,
            'guarded_adoption_history':db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],
            'cancelled_adoptions':db.execute('SELECT count(*) FROM sync_adoptions WHERE cancelled=1').fetchone()[0],
            'progress_count':events(db)[0]['progress_count'],'source_revision_history':len(events(db)[0].get('source_revisions',[])),
            'operation_receipts':db.execute('SELECT count(*) FROM sync_operations').fetchone()[0],
            'input_bytes_fixture_legacy':old_bytes,'input_bytes_fixture_with_dependency_context':len(new_prompt.encode()),
            'atomic_failure_rollback':True,'sqlite_reopen_recovery':True,'foreign_key_errors':db.execute('PRAGMA foreign_key_check').fetchall()}
        db.close();return result

def run(root,dataset_raw,review_raw):
    before=hashes(root);dataset,records=validate_records(review_raw,dataset_raw)
    prior=evaluate(root,dataset,batch_size=(4,64));projections=[]
    def setup(db):
        prepare_articles(db,dataset_raw,review_raw);initialize(db)
        binding=bind_operator_reviews(db,dataset_raw,review_raw)
        if len(binding['bound'])!=15:raise AssertionError('operator binding incomplete')
    def project(db,sample,state):
        with atomic(db):
            for event in events(db):refresh_links(db,event['event_id'])
        for event in events(db):
            expected={version_id(a) for a in event.get('articles',[])}
            actual={r[0] for r in db.execute('SELECT version_id FROM sync_links WHERE event_id=? AND is_current=1',(event['event_id'],))}
            if actual!=expected:raise AssertionError('live article projection mismatch')
        projections.append(True)
    current=evaluate(root,dataset,batch_size=(4,64),db_hook=setup,db_final_hook=project)
    rows=[]
    for old,new in zip(prior['rows'],current['rows']):
        fields=('sample_id','target_rank','target_retained','staged_status','staged_total_bytes','staged_batches','staged_changed_event_ids')
        if any(old[k]!=new[k] for k in fields):raise AssertionError('compatibility changed')
        row=next(r for r in dataset['rows'] if r['sample_id']==new['sample_id'])
        rows.append(new|{'cohort':cohort(row,records.get(new['sample_id']))})
    probes={'operator_records':saved_operator_probe(root,dataset_raw,review_raw),
            'shared_article':shared_source_probe(root,dataset_raw),'stored_revision':stored_revision_probe(root),'recovery':recovery_probe()}
    if before!=hashes(root):raise AssertionError('snapshot JSON changed')
    return {'schema':'news-now-revision-sync-replay-v1','snapshot_revision':dataset['snapshot_revision'],
        'compatibility_replay':{'mode':'unchanged staged guards plus binding and post-judgment link projection; not complete scheduler routing through sync API',
            'samples':33,'outcomes_candidates_input_bytes_equal_to_prior':True,'post_projection_verified':len(projections),
            'rows':rows,'summary':current['summary']},'probes':probes,
        'json_hashes_unchanged':True,'real_api_calls':0,'actual_ai_accuracy':'unconfirmed','actual_api_cost':'unconfirmed',
        'limitations':['saved metadata variants do not establish their temporal ordering',
            'all newly provided bodies and source mutations in probes are explicitly synthetic',
            'literal quotation matching is not publisher authenticity or incident identity verification',
            'live sync API adoption/cancellation is tested separately from 33-row staged compatibility replay',
            'raw cache/SQL reservation tests do not prove exactly-once real HTTP billing; uncertain responses stop',
            'no production export, full scheduler integration, multi-worker stress test or persistent Actions DB migration']}

def markdown(result):
    lines=['# 記事改訂・判断同期：検証結果','',
        '固定保存データの互換比較と、改訂・障害を起こす模擬検証を分けました。本文・訂正を実ニュースの新事実として扱っていません。',
        '','## 同期APIの検証','',
        '- 保存された実際の公開日時違いの版を使って、旧版を残しつつ判断を再確認待ちへ移すことを確認。どちらが実際に先かは推測しません。',
        '- 実在のreal-022のURL/2事件IDに模擬本文を設定。本文変更で関連AI参照2件と模擬運営者判断1件の計3件が再確認待ち。無関係な判断と回答キャッシュは維持。',
        '- 見出しだけ/公開日時だけの変更は、それぞれ該当フィールド参照1件のみ影響。実AIジョブは送信するメタデータ全項目も依存関係へ含めます。',
        '- 取得失敗では元の版と判断を保持し取得再試行待ちだけ記録。削除は旧本文・引用を残して参照判断を再確認待ちにします。',
        '- 提供された運営者記録15件を原文のまま別表へ保存し依存を設定。022の模擬見出し変更で022だけが再確認待ち、残り14件はcurrentを維持。評価参照であり本番振り分けには反映しません。',
        '','## 訂正・取消し・再採用と障害復旧','',
        '模擬案件で既存安全検証を通る採用→記事訂正→再採用→訂正分の取消し→再採用を実行。新旧の記事版、証拠、AI判断、操作履歴を残しました。',
        '採用履歴3件と取消し1件は明示的な操作の履歴で、進展は1回のまま。異なる入力2種類に予約2回・模擬回答2回、再実行と再採用による追加予約0でした。',
        '途中の版保存・判断/リンク保存・取消し後で失敗させて巻戻し、コミット後の応答消失は操作IDの完了記録で復旧。SQLiteを閉じて開き直した後も二重処理しません。',
        '応答を保存できたか不明な隙間では、自動でAI再呼出しをせず要照合に停止。前日の未使用予約も当日の予算代わりに使わず確認待ちにします。',
        '','## 引用照合','',
        '模擬原文で完全一致・不一致・版変更後の旧引用を区別。全角半角・記号の補正や言い換えはしません。旧引用は元の保存版に対して照合でき、現在版にはstaleを返します。',
        '原文未取得では未検証。14出典の未提供引用・確認日時は元の空欄/nullのままです。文字一致しても事件の同一性や出典の真正性を承認せず、既存ガードは必須です。',
        '','## 現行方式との互換比較','',
        '33組の保存済み回答/模擬AIによる候補順位・保持・判定・入力は前段の試作から不変。現行は全件提示、新試作は最初4件→残りまとめ。',
        '|区分|現行入力バイト|試作入力バイト|現行保留|試作保留|','|---|---:|---:|---:|---:|']
    groups=[('両本文確認7組',{'operator_same_both'}),('本文に未確認部分あり6組',{'operator_same_partial','operator_same_neither'}),
            ('運営者保留2組',{'operator_hold_partial','operator_hold_neither'}),('従来の暫定同一9組',{'provisional_same'}),('従来の暫定別事件9組',{'provisional_different'})]
    for label,cohorts in groups:
        rows=[r for r in result['compatibility_replay']['rows'] if r['cohort'] in cohorts]
        lines.append(f"|{label}|{sum(r['legacy_total_bytes'] for r in rows):,}|{sum(r['staged_total_bytes'] for r in rows):,}|{sum(r['legacy_status']!='already_saved' for r in rows)}|{sum(r['staged_status']!='accepted' for r in rows)}|")
    recovery=result['probes']['recovery']
    lines+=['','候補保持は両本文確認7組で新旧7/7、試作は全て1位。全体の保留は22/33。暫定別事件9組への模擬誤統合は新旧0です。',
        'この33組比較は、未改訂の既存段階照合に依存の読込/照合後の投影を追加した互換試験です。段階照合の全呼出しを新同期APIへ接続した精度・性能試験ではありません。',
        f"同期APIの模擬1件では、送信する依存情報の追加により入力が{recovery['input_bytes_fixture_legacy']:,}→{recovery['input_bytes_fixture_with_dependency_context']:,}バイトに増加。実際の同期APIが前段と同じ入力サイズになるとは主張しません。",
        '実AI精度・実API料金は未測定。今回の同期による費用削減率は算出しません。',
        '','## 本番移行の阻止条件','',
        '1. 段階照合スケジューラの全経路、既存のAI不成立キャッシュ/事実への依存付け、公開出力への再確認表示は未接続。未知の旧依存を推測して有効扱いしない。',
        '2. 本文だけ変更しRSSが不変の場合、既存source_saved判定を迂回せず保留可能なまま。本文根拠を既存ガードへ安全に接続する工程は未完成。',
        '3. 実原文・実改訂の十分な標本、実AI、複数ワーカーの競合負荷、GitHub Actionsでの永続化/バックアップ/復旧は未確認。',
        '4. 実HTTPの応答不明時の請求照合、日跨ぎ予約の整理は自動化せず確認待ち。費用上限や安全基準を緩めて再試行しない。',
        '','今回は試作と検証のみで、本番移行・mainマージは行いません。']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,default=ROOT);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    root=a.snapshot.resolve();out=a.output_dir.resolve()
    if out.is_relative_to(root):p.error('output must be outside snapshot')
    here=Path(__file__).parent;dataset_raw=(here/'evaluation.json').read_bytes();review_raw=(here/'operator_review_records.json').read_bytes();attempts=[]
    def deny(*args,**kw):attempts.append(True);raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=run(root,dataset_raw,review_raw)
    if attempts:raise AssertionError('network attempted')
    result['network_attempts']=0;out.mkdir(parents=True,exist_ok=True)
    (out/'sync_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'SYNC_RESULTS.md').write_text(markdown(result))
    print(json.dumps({'probes':result['probes'],'json_hashes_unchanged':True},ensure_ascii=False))

if __name__=='__main__':main()
