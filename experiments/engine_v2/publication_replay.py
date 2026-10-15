"""Offline publication-control rehearsal. Never authorizes production data."""
import argparse
import copy
import hashlib
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
from engine import connect,import_snapshot,events,dumps
from compare import hashes
from test_publication import PublicationTests
from test_tracking import NOW,report
from event_tracker import TRACKING_RESPONSE
import publication as pub
import revision_sync as sync
import end_to_end as flow


def baseline(root):
    db=connect();import_snapshot(db,root);pub.initialize(db)
    before=events(db);registry=json.loads((root/'data_corrections.json').read_text())
    legacy=flow.legacy_public(db,registry);result=pub.project(db,registry)
    assert before==events(db) and not result['events'] and not result['notices']
    assert db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0]==0
    data={'source_events':len(before),'existing_public_events':len(legacy),'approved_export_events':len(result['events']),
          'automatically_imported_approvals':0,'approval_holds':len(result['holds']),
          'existing_public_unapproved':sum(h['reason']=='event_identity_unapproved' for h in result['holds']),
          'event_ids_preserved':True,'article_urls_preserved':True,'history_preserved':True,
          'legacy_vs_approved_diff':flow.compare_public(legacy,result['events'])}
    db.close();return data


def synthetic_lifecycle():
    fixture=PublicationTests();fixture.setUp();db=fixture.db;phases=[];artifacts={}
    def capture(name):
        data=pub.project(db,{});artifacts[name]={'events':data['events'],'notices':data['notices'],'scope':'isolated_simulation','deployable':False}
        phases.append({'phase':name,'events':len(data['events']),'articles':sum(e['article_count'] for e in data['events']),
            'progress':sum(e['progress_count'] for e in data['events']),
            'current_stages':[e['current_stage'] for e in data['events']],
            'notices':len(data['notices']),'approval_states':dict(db.execute('SELECT status,count(*) FROM pub_state GROUP BY status'))})
    try:
        capture('unapproved');identity,articles=fixture.basic();capture('identity_and_articles')
        stage=fixture.approve('stage','field','current_stage')
        progress=events(db)[0]['progress_history'][0];fixture.approve('progress','progress',pub.key_for('progress',progress))
        generation=flow.prepare_export(db,{})
        with tempfile.TemporaryDirectory(prefix='news-now-publication-check-') as out:
            result=flow.materialize(db,generation,out,snapshot_root=fixture.root)
            loaded=flow.load_approved_simulation(db,generation,result['directory']);assert len(loaded['events'])==1
        capture('approved_details')
        fixture.withdraw(stage,'withdraw-stage');capture('field_withdrawn')
        fixture.withdraw(identity,'withdraw-event');capture('event_withdrawn')
        new=fixture.approve('reapprove-identity','event','identity',supersedes=identity);capture('event_reapproved')
        changed=copy.deepcopy(fixture.source);changed['title']+=' SYNTHETIC 訂正'
        sync.change_source(db,'correct-source',changed,sync.head(db,fixture.url)['state_id'],NOW,body_text=fixture.body);capture('source_corrected_recheck')
        fixture.source=changed;fixture.g['articles']=[changed];fixture.proofs[fixture.url]=sync.add_evidence(db,'e1',fixture.url,'title',changed['title'])
        fixture.did,_=fixture.guard('guard-corrected')
        for index,a in enumerate(events(db)[0]['articles']):fixture.approve('reapprove-article:'+str(index),'article',a['url'],supersedes=articles[index])
        fixture.approve('reapprove-identity-after-correction','event','identity',supersedes=new)
        fixture.approve('reapprove-stage','field','current_stage',supersedes=stage);capture('corrected_reapproved')
        flow.prepare_export(db,{})
        columns=[r[1] for r in db.execute('PRAGMA table_info(pub_approvals)')]
        approvals=[]
        for row in db.execute('SELECT * FROM pub_approvals'):
            value=dict(zip(columns,row));value['content']=json.loads(value['content']);value['proof']=json.loads(value['proof']);approvals.append(value)
        output={'basis':'synthetic fixtures and explicitly simulated operator approval only','phases':phases,
                'approvals':approvals,'history':[list(r) for r in db.execute('SELECT * FROM pub_history')],
                'preserved_event_ids':[e['event_id'] for e in events(db)],
                'source_revision_history':sum(len(e.get('source_revisions',[])) for e in events(db)),
                'foreign_key_errors':db.execute('PRAGMA foreign_key_check').fetchall()}
        return output,artifacts
    finally:fixture.tearDown()


def shared_source():
    fixture=PublicationTests();fixture.setUp()
    try:
        db=fixture.db;body=sync.add_evidence(db,'e1',fixture.url,'body','100万円')
        sync.cancel(db,'reset-body',fixture.adoption,NOW)
        fixture.did,fixture.adoption=fixture.guard('body-dependent-A',list(fixture.proofs.values())+[body])
        a,_=fixture.basic();b,_,_=fixture.second_event();pub.project(db,{})
        sync.change_source(db,'body-change',fixture.source,sync.head(db,fixture.url)['state_id'],NOW,body_text=fixture.body.replace('100','120'))
        result={'basis':'synthetic A/B with one shared article; A consumes body, B title only',
                'after_body_change':{'event_A':fixture.state(a),'event_B':fixture.state(b)},
                'events_exported':[e['event_id'] for e in pub.project(db,{})['events']]}
        changed=copy.deepcopy(fixture.source);changed['title']+=' SYNTHETIC見出し訂正'
        sync.change_source(db,'heading-change',changed,sync.head(db,fixture.url)['state_id'],NOW,body_text=fixture.body.replace('100','120'))
        result['after_heading_change']={'event_A':fixture.state(a),'event_B':fixture.state(b)}
        assert result['after_body_change']=={'event_A':'needs_recheck','event_B':'approved'}
        assert result['after_heading_change']=={'event_A':'needs_recheck','event_B':'needs_recheck'}
        return result
    finally:fixture.tearDown()


def body_only_blocker():
    fixture=PublicationTests();fixture.setUp()
    try:
        db=fixture.db;body=sync.add_evidence(db,'e1',fixture.url,'body','100万円')
        sync.cancel(db,'reset-body',fixture.adoption,NOW);fixture.did,fixture.adoption=fixture.guard('body-judgment',list(fixture.proofs.values())+[body])
        aid=fixture.approve('body-approval','field','current_stage',evidence_ids=[body])
        sync.change_source(db,'body-only',fixture.source,sync.head(db,fixture.url)['state_id'],NOW,body_text=fixture.body.replace('100','120'))
        fresh=sync.add_evidence(db,'e1',fixture.url,'body','120万円')
        jid=sync.make_job(db,'e1',fixture.g,[fresh],NOW)
        answer=copy.deepcopy(TRACKING_RESPONSE);answer.update(report(fixture.source));sync.call_mock(db,jid,lambda p:answer,NOW)
        outcome=sync.adopt(db,'new-body-only-judgment',jid,NOW)['status']
        try:fixture.approve('body-reapproval','field','current_stage',evidence_ids=[fresh],supersedes=aid)
        except ValueError:blocked=True
        else:blocked=False
        assert outcome=='already_saved' and blocked
        return {'basis':'synthetic unchanged URL/title/description/published, body only changed',
                'prior_approval_status':fixture.state(aid),'new_legacy_guard_outcome':outcome,'reapproval_safely_blocked':blocked,
                'remaining_work':'body-only judgment revalidation path; do not bypass existing preflight or reuse stale accepted judgment'}
    finally:fixture.tearDown()


def run(root):
    before=hashes(root);started=time.perf_counter();life,artifacts=synthetic_lifecycle()
    result={'snapshot_revision':'f4b831d3fa1f514b2713ecca5acebc8a6aaaf99c','existing_data':baseline(root),
        'synthetic_lifecycle':life,'shared_source':shared_source(),'body_only_blocker':body_only_blocker(),
        'json_hashes_before':before,'json_hashes_unchanged':before==hashes(root),'real_api_calls':0,
        'production_approval_operations':0,'simulated_only':True,'elapsed_ms':(time.perf_counter()-started)*1000}
    assert result['json_hashes_unchanged'];return result,artifacts


def markdown(result):
    b=result['existing_data'];lines=['# 公開承認・取消し・再承認の隔離検証','',
        '実際の公開・承認操作は0件。表中の承認はすべて架空の記事・判断と `simulation:operator` の模擬操作です。既存の運営者評価を正解や公開承認に変換していません。','',
        f"保存事件{b['source_events']}件・既存公開{b['existing_public_events']}件を保持。承認の自動移行0件、承認済み公開JSONは{b['approved_export_events']}件。既存34件は引き続き未承認です。",'',
        '|模擬段階|公開対象事件|承認済み記事|進展回数|現在地|','|---|---:|---:|---:|---|']
    for p in result['synthetic_lifecycle']['phases']:lines.append(f"|{p['phase']}|{p['events']}|{p['articles']}|{p['progress']}|{','.join(p['current_stages']) or '非表示'}|")
    lines+=['','事件名と記事の承認だけでは、現在地・進展・予定・訂正を公開しません。項目の承認を取り消すと、その項目だけ確認中または非表示になります。事件の基本情報を取り消すと事件本体は出力されず、別の公開状態JSONに一般的な取消し案内を残します。',
        '',f"共有記事の本文変更後: {result['shared_source']['after_body_change']}。見出し変更後: {result['shared_source']['after_heading_change']}。本文全体を使う承認同士では、引用部分が同じでも本文版が変われば再確認します。無検証で新しい版へ承認を付け替えません。",'',
        '## 障害・古い承認の扱い','',
        '承認・取消し・根拠失効と履歴をSQLiteの同じ保存範囲に記録。保存前失敗は全体を戻し、保存後の応答消失は同じ操作IDで再開。古い操作の結果を取得しても、その承認は再有効化しません。',
        '公開ファイル生成の途中失敗も同じ世代で再開。承認・根拠が変わった後の古いファイルは専用読取関数が拒否します。比較用JSONを改名しmanifestのハッシュを書き換えた場合も、SQLiteに保存した承認済み世代との一致検査で拒否します。',
        '', '## 残る阻止条件','',
        '- 本文だけの変更では旧処理の重複防止が already_saved を返し、新しい承認根拠に使える採用判断を作れない場合があります。旧判断の流用で回避せず、再承認を止めることを確認済みです。本文版を理解した再検証経路が必要です。',
        '- 承認者は模擬ID。本人確認・権限管理・スマホ管理UI・実際の公開配信には接続していません。',
        '- 既存サイトは公開状態JSONをまだ表示しません。取消し理由の安全な定型表示と履歴表示の設計だけで、実画面への接続は未実装です。',
        '- 根拠失効はその根拠IDに対して恒久記録。失効取消しの管理UIは未実装で、新しい根拠版・判断・明示再承認が必要です。',
        '- 引用の文字一致は真偽・事件同一性・記事の真正性の保証ではありません。実データの原文引用・確認日時の不足を補っていません。',
        '- 項目の一部だけを公開する際の文章全体の意味・プライバシー・実ブラウザー表示、永続DB復旧・バックアップは未検証です。',
        '',f"実通信0、本番承認0、元JSONハッシュ不変: {result['json_hashes_unchanged']}。単発の模擬処理時間 {result['elapsed_ms']/1000:.3f}秒。本番移行の可否は今回判断していません。"]
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=ROOT);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    root=args.snapshot.resolve();out=args.output_dir.resolve()
    if out.is_relative_to(root):parser.error('output must be outside input snapshot')
    attempts=[]
    def deny(*a,**kw):attempts.append(True);raise AssertionError('real networking forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):result,artifacts=run(root)
    if attempts:raise AssertionError('real network attempted')
    result['network_attempts']=0;out.mkdir(parents=True,exist_ok=True)
    (out/'publication_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'PUBLICATION_RESULTS.md').write_text(markdown(result))
    for name,value in artifacts.items():(out/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'existing':result['existing_data'],'phases':result['synthetic_lifecycle']['phases'],'shared':result['shared_source'],'blocker':result['body_only_blocker'],'json_hashes_unchanged':True},ensure_ascii=False))

if __name__=='__main__':main()
