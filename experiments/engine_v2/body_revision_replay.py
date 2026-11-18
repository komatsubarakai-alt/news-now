"""Read-only compatibility plus explicitly synthetic body-revision lifecycle."""
import argparse
import copy
import json
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from test_content_revision import ContentRevisionTests
from test_tracking import NOW
from engine import connect,events,dumps
from compare import hashes
from pending_triage import preflight
from publication_replay import baseline
from failed_response import digest
import content_revision as revision
import revision_sync as sync
import publication as pub
import end_to_end as flow


def lifecycle():
    f=ContentRevisionTests();f.setUp();db=f.db;phases=[];artifacts={}
    def capture(name):
        output=pub.project(db,{});event=events(db)[0]
        phases.append({'phase':name,'public_events':len(output['events']),
            'canonical_summary':event['summary'],'public_summaries':[e['summary'] for e in output['events']],
            'internal_progress':event['progress_count'],'public_progress':sum(e['progress_count'] for e in output['events']),
            'mock_calls_after_initial':len(f.calls),'adoptions':db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],
            'article_metadata_versions':[sync.version_id(a) for a in event['articles']],
            'body_binding':revision.bindings(db,'e1').get(f.url),
            'approval_states':dict(db.execute('SELECT status,count(*) FROM pub_state GROUP BY status'))})
        artifacts[name]={'scope':'isolated_simulation','deployable':False,'events':output['events'],'notices':output['notices']}
    try:
        initial_event=events(db)[0];article_approval=f.approve('article','article',f.url,f.old_proof,f.old_did)
        identity_approval=f.approve('identity','event','identity',f.title_proof,f.old_did)
        flow.prepare_export(db,{});capture('old_edition_approved')
        f.change();newproof=f.quote();capture('body_changed_unapproved')
        known={a['url']:a for e in events(db) for a in e['articles']}
        old_policy=preflight(f.g,known,{},{});assert old_policy=='already_saved'
        result=f.run_revision(proof=newproof);assert result['status']=='accepted';capture('new_judgment_not_public')
        try:f.approve('old-judgment-reuse','field','summary',f.old_proof,f.old_did)
        except ValueError:old_reuse_blocked=True
        else:old_reuse_blocked=False
        title=sync.add_evidence(db,'e1',f.url,'title',f.article['title'])
        quote='金額は120万円。容疑者を起訴。';progress_proof=sync.add_evidence(db,'e1',f.url,'body',quote)
        article2=f.approve('new-article','article',f.url,newproof,result['decision_id'],article_approval)
        identity2=f.approve('new-identity','event','identity',title,result['decision_id'],identity_approval)
        summary=f.approve('new-summary','field','summary',progress_proof,result['decision_id'])
        progress=events(db)[0]['progress_history'][-1]
        f.approve('new-progress','progress',pub.key_for('progress',progress),progress_proof,result['decision_id'])
        generation=flow.prepare_export(db,{})
        with tempfile.TemporaryDirectory(prefix='news-now-body-public-') as out:
            saved=flow.materialize(db,generation,out,snapshot_root=f.root)
            assert flow.load_approved_simulation(db,generation,saved['directory'])['events'][0]['summary']=='金額は120万円。'
        capture('new_edition_explicitly_approved');budget=f.budget()
        assert f.run_revision(proof=newproof)['decision_id']==result['decision_id']
        no_change=f.run_revision('same-content-new-request',proof=newproof);assert no_change['status']=='already_saved'
        assert budget==f.budget();capture('unchanged_rerun')
        sync.cancel(db,'cancel-new-adoption',result['adoption_id'],NOW);capture('judgment_cancelled')
        again=f.run_revision('readopt',proof=newproof);assert again['status']=='accepted';capture('readopted_not_reapproved')
        assert budget==f.budget() and len(f.calls)==1
        rows=[];columns=[r[1] for r in db.execute('PRAGMA table_info(sync_revision_validations)')]
        for row in db.execute('SELECT * FROM sync_revision_validations'):
            value=dict(zip(columns,row));value['source_editions']=json.loads(value['source_editions']);rows.append(value)
        output={'basis':'synthetic fixture, explicit mock AI and simulated operator only','legacy_metadata_policy_after_body_change':old_policy,
            'new_status':result['status'],'old_decision_id':f.old_did,'new_decision_id':result['decision_id'],
            'old_judgment_reuse_blocked':old_reuse_blocked,'phases':phases,'validation_records':rows,
            'mock_calls_for_revision_and_readoption':len(f.calls),'revision_model_input_utf8_bytes':sum(len(p.encode()) for p in f.calls),
            'reservations_including_initial':db.execute('SELECT count(*) FROM sync_reservations').fetchone()[0],
            'canonical_metadata_unchanged':initial_event['articles']==events(db)[0]['articles'],
            'body_view_marker_never_saved':revision.MARKER not in dumps(events(db)),
            'old_body_edition_retained':db.execute('SELECT count(*) FROM sync_content_editions').fetchone()[0]>=2,
            'source_revision_history':len(events(db)[0].get('source_revisions',[])),
            'foreign_key_errors':db.execute('PRAGMA foreign_key_check').fetchall()}
        assert old_reuse_blocked and output['canonical_metadata_unchanged'] and output['body_view_marker_never_saved']
        return output,artifacts
    finally:f.tearDown()


def shared_probe():
    f=ContentRevisionTests();f.setUp()
    try:
        b=f.shared_event(body_dependent=False);f.change();a=f.run_revision()
        result={'basis':'synthetic shared URL, A body-dependent and B title-only','event_A':a['status'],
                'event_B_judgment_status':f.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(b,)).fetchone()[0],
                'event_ids_preserved':sorted(e['event_id'] for e in events(f.db))}
        assert result['event_B_judgment_status']=='current';return result
    finally:f.tearDown()


def recovery():
    f=ContentRevisionTests();f.setUp()
    try:
        f.change();proof=f.quote();before=events(f.db);bindings=revision.bindings(f.db,'e1')
        try:f.run_revision(proof=proof,failpoint='before_receipt')
        except RuntimeError:pass
        else:raise AssertionError('missing failure injection')
        assert events(f.db)==before and revision.bindings(f.db,'e1')==bindings
        budget=f.budget()
        with tempfile.TemporaryDirectory(prefix='news-now-body-restart-') as temporary:
            path=Path(temporary)/'shadow.sqlite';disk=connect(str(path));f.db.backup(disk);disk.close();disk=connect(str(path))
            result=revision.rejudge(disk,'rejudge','e1',f.g,[proof],f.mock,NOW)
            repeated=revision.rejudge(disk,'rejudge','e1',f.g,[proof],f.mock,NOW)
            assert result==repeated and result['status']=='accepted'
            assert disk.execute('SELECT * FROM budget').fetchall()==budget
            value={'basis':'synthetic interrupted adoption and SQLite restart','rollback_exact':True,'restart_status':result['status'],
                   'mock_calls':len(f.calls),'new_reservations_after_retry':0,'operation_idempotent':True,
                   'adoptions_including_initial':disk.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],
                   'foreign_key_errors':disk.execute('PRAGMA foreign_key_check').fetchall()}
            disk.close();return value
    finally:f.tearDown()


def markdown(result):
    lines=['# 本文改訂後の再判定・再承認','',
        'すべて通信なし。本文・AI・承認は架空ケースで検証し、実記事の正解率・実AI料金は測定していません。',
        '', '## 原因と修正','',
        '従来のalready_savedはURL・見出し・説明・公開日時だけを比較し、別管理の本文を見ていませんでした。本文だけ変わっても、同じ保存済み記事として判定を打ち切っていました。',
        '試作側に、メタデータ版・本文ハッシュ・実際の変更世代から成る記事内容版と、事件ごとの採用済み版を追加。新旧の保存原文を検証用ビューへ渡し、従来の重複・同一性・矛盾・進展・保存前検証をすべて実行します。公開データのRSS説明を本文に書き換えたり、force/検証省略フラグを使ったりしません。',
        '', '|模擬段階|公開事件|内部進展|承認済み公開進展|追加模擬AI回数|','|---|---:|---:|---:|---:|']
    for p in result['lifecycle']['phases']:lines.append(f"|{p['phase']}|{p['public_events']}|{p['internal_progress']}|{p['public_progress']}|{p['mock_calls_after_initial']}|")
    c=result['compatibility'];b=result['existing_data']
    lines+=['','新版の判定採用だけでは公開しません。新版に対する明示承認後だけ出力し、判断取消し後の再採用にも再承認が必要です。進展数は模擬回答と既存ガードによる結果で、本文変更を自動的に進展へ数える方式ではありません。',
        '', '## 保存済みデータとの互換性','',
        f"固定入力の事件{b['source_events']}件、既存公開{b['existing_public_events']}件を保持。既存34件の承認自動移行は0、承認済み公開出力0件。新旧33組の内部事件データ一致{c['internal_equal']}/33、比較用公開出力一致{c['preview_equal']}/33。", 
        f"33組の入力サイズ: 旧{c['legacy_bytes']:,} / 段階方式{c['new_bytes']:,} UTF-8 bytes。実料金削減ではありません。",'',
        '## 履歴・障害・共有記事','',
        f"本文改訂と取消し後の再採用で追加の模擬AIは{result['lifecycle']['mock_calls_for_revision_and_readoption']}回。再実行・再採用の追加予約は0。旧本文・旧判断・取消し・公開承認履歴を保持します。", 
        f"共有URL: {result['shared']}。本文に依存しないBの判断は維持。双方が本文に依存する場合の事件別旧版照合は追加テストで確認しています。", 
        f"障害試験: {result['recovery']}。",'',
        '## 残る阻止条件','',
        '- 実承認者の認証・権限、スマホUI、取消し状態の実画面表示・配信への接続。',
        '- 実記事本文の取得・抽出品質、提供されていない引用・確認日時の補完、実AI精度と実API料金。',
        '- 長い本文では入力が増える。予算が不足した場合は記事版・依頼を残して停止し、上限は増やさない。',
        '- 対象事件が既存記事を所有しない場合、根拠不足、本文未取得、運営者確認待ちは引き続き保留。自動的な事件統合・新事件登録は行わない。',
        '- 旧試作DBの採用済み本文版の対応が不足する場合は明示確認が必要。永続DB・全運用データ移行・バックアップ・実運用復旧は未検証。',
        '',f"元JSONハッシュ不変: {result['json_hashes_unchanged']}、実通信0、本番公開・承認0。模擬ライフサイクル等の単発時間{result['elapsed_ms']/1000:.3f}秒（33組比較時間は別）。"]
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=ROOT);parser.add_argument('--compatibility-report',type=Path,required=True);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    root=args.snapshot.resolve();out=args.output_dir.resolve()
    if out.is_relative_to(root):parser.error('output must be outside input snapshot')
    before=hashes(root);compat=json.loads(args.compatibility_report.read_text())
    if compat['snapshot_revision']!='f4b831d3fa1f514b2713ecca5acebc8a6aaaf99c' or not compat['json_hashes_unchanged']:parser.error('incompatible comparison report')
    # Compare the same imported saved files, not a silently refreshed main JSON.
    for name in ('events.json','tracking_pending.json','news_status.json','tracking_triage.json','status_pending.json','operator_review_decisions.json','data_corrections.json','ai_budget.json','ai_response_cache.json','tracking_review_recent.json'):
        if compat['json_hashes_before'].get(name)!=before.get(name):parser.error('comparison report uses another source snapshot: '+name)
    attempts=[]
    def deny(*a,**kw):attempts.append(True);raise AssertionError('real networking forbidden')
    started=time.perf_counter()
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        life,artifacts=lifecycle();shared=shared_probe();restored=recovery();saved=baseline(root)
    if attempts:raise AssertionError('real network attempted')
    rows=compat['comparison']['rows']
    result={'snapshot_revision':compat['snapshot_revision'],'lifecycle':life,'shared':shared,'recovery':restored,'existing_data':saved,
        'compatibility':{'rows':len(rows),'internal_equal':sum(r['event_payloads_equal'] for r in rows),
                         'preview_equal':sum(not r['public_comparison']['differences'] for r in rows),
                         'legacy_bytes':sum(r['legacy_total_bytes'] for r in rows),'new_bytes':sum(r['staged_total_bytes'] for r in rows)},
        'json_hashes_before':before,'json_hashes_unchanged':before==hashes(root),'elapsed_ms':(time.perf_counter()-started)*1000,
        'network_attempts':0,'real_api_calls':0,'real_api_cost':'unconfirmed','real_model_accuracy':'unconfirmed','production_publications_or_approvals':0}
    assert result['json_hashes_unchanged'];out.mkdir(parents=True,exist_ok=True)
    (out/'body_revision_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'BODY_REVISION_RESULTS.md').write_text(markdown(result))
    for name,data in artifacts.items():(out/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'compatibility':result['compatibility'],'phases':life['phases'],'recovery':restored,'json_hashes_unchanged':True},ensure_ascii=False))

if __name__=='__main__':main()
