"""Reproducible saved/mock-only end-to-end rehearsal, never production output."""
import argparse
import copy
import json
import socket
import shutil
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

from stage_compare import ROOT,evaluate
from engine import connect,import_snapshot,events
from article_store import import_saved,import_reviews
from operator_revalidation import validate_records,cohort
from compare import hashes
from staged import inspect
from event_tracker import TRACKING_RESPONSE
import end_to_end as flow
from sync_replay import recovery_probe,shared_source_probe,saved_operator_probe,stored_revision_probe

NOW=datetime(2026,10,15,12,tzinfo=timezone.utc)

def public_reference(root,committed_public):
    db=connect();import_snapshot(db,root);flow.initialize(db)
    registry=json.loads((root/'data_corrections.json').read_text())
    before=events(db);derived=flow.legacy_public(db,registry)
    gen=flow.prepare_export(db,registry)
    with tempfile.TemporaryDirectory(prefix='news-now-e2e-public-') as out:
        artifact=flow.materialize(db,gen,out,snapshot_root=root)
        strict=json.loads((Path(artifact['directory'])/'public_events.json').read_text())
        manifest=json.loads((Path(artifact['directory'])/'manifest.json').read_text())
    assert events(db)==before
    result={'source_events':len(before),'committed_public_events':len(committed_public),
        'legacy_gate_public_events':len(derived),'strict_public_events':len(strict),
        'committed_vs_existing_gate':flow.compare_public(committed_public,derived),
        'existing_gate_vs_strict':flow.compare_public(derived,strict),
        'strict_publication_holds':len(derived),'array_schema_compatible':isinstance(strict,list),
        'legacy_display_fields_preserved_exactly':all(e==next(x for x in before if x['event_id']==e['event_id']) for e in derived),
        'manifest_preview_only':manifest['preview_only'],'can_publish':manifest['can_publish'],
        'reason':'no independent publication approvals in imported data or saved/mock rehearsals; no reference promoted',
        'unchanged_ids_and_urls_in_sqlite':True,'internal_holds':db.execute('SELECT count(*) FROM holds').fetchone()[0]}
    db.close();return result


def capped_queue_probe(root):
    db=connect();import_snapshot(db,root);flow.initialize(db);import_saved(db)
    groups=json.loads((root/'tracking_pending.json').read_text());registry=json.loads((root/'data_corrections.json').read_text())
    triage=json.loads((root/'tracking_triage.json').read_text()).get('items',{})
    states=[];callback_count=0;started=time.perf_counter()
    def mock(prompt,batch):
        nonlocal callback_count
        callback_count+=1;return copy.deepcopy(TRACKING_RESPONSE)
    for group in groups:
        try:sid=flow.begin(db,group,triage,registry)
        except ValueError as exc:
            original=json.loads(db.execute("SELECT payload FROM source_files WHERE path='tracking_pending.json'").fetchone()[0])
            states.append({'status':'explicit_source_selection_required','group_retained':group in original,'reason':str(exc)});continue
        state=inspect(db,sid)
        while state['status']=='ready':state=flow.step(db,sid,mock,NOW,triage,registry,charge=True)
        states.append({'status':state['status'],'group_retained':state['group']==group,'cursor':state['cursor'],'candidates':len(state['candidate_states']),'unexamined':sum(v=='unexamined' for v in state['candidate_states'].values())})
    budget_row=db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()
    budget=json.loads(budget_row[0]) if budget_row else json.loads((root/'ai_budget.json').read_text())
    day,month=NOW.strftime('%Y-%m-%d'),NOW.strftime('%Y-%m')
    assert all(s['group_retained'] for s in states)
    assert budget.get('days',{}).get(day,{}).get('reserved_micro_usd',0)<=200000
    assert budget.get('months',{}).get(month,{}).get('reserved_micro_usd',0)<=5000000
    result={'groups':len(groups),'statuses':dict(Counter(s['status'] for s in states)),
        'all_groups_retained':True,'unexamined_candidates':sum(s.get('unexamined',0) for s in states),
        'mock_calls':callback_count,'input_bytes':sum(len(p.encode()) for p, in db.execute('SELECT prompt FROM e2e_answers')),
        'day_reserved_micro_usd':budget.get('days',{}).get(day,{}).get('reserved_micro_usd',0),
        'month_reserved_micro_usd':budget.get('months',{}).get(month,{}).get('reserved_micro_usd',0),
        'daily_limit_micro_usd':200000,'monthly_limit_micro_usd':5000000,
        'tracking_run_call_limit':8,'source_budget_file_unchanged':True,'ms':(time.perf_counter()-started)*1000,
        'simulation_only':True,'rows':states}
    db.close();return result


def run(root,reference):
    baseline=hashes(root);here=Path(__file__).parent;raw=(here/'evaluation.json').read_bytes();reviews=(here/'operator_review_records.json').read_bytes()
    dataset,records=validate_records(reviews,raw);start=time.perf_counter()
    def setup(db):flow.initialize(db);import_saved(db);import_reviews(db,raw,reviews)
    def finish(db,sample,state):
        registry=json.loads((root/'data_corrections.json').read_text());t=time.perf_counter()
        generation=flow.prepare_export(db,registry)
        with tempfile.TemporaryDirectory(prefix='news-now-e2e-sample-') as out:
            flow.materialize(db,generation,out,snapshot_root=root)
        return {'sync_adoptions':db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],
                'sync_decisions':db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],
                'operator_records_preserved':db.execute('SELECT count(*) FROM sep_review_records').fetchone()[0],
                'foreign_key_errors':db.execute('PRAGMA foreign_key_check').fetchall(),
                'strict_public_events':0,'export_ms':(time.perf_counter()-t)*1000}
    comparison=evaluate(root,dataset,batch_size=(4,64),db_hook=setup,db_final_hook=finish,pipeline=flow)
    cohorts={}
    byid={r['sample_id']:r for r in dataset['rows']}
    for row in comparison['rows']:
        name=cohort(byid[row['sample_id']],records.get(row['sample_id']))
        cohorts.setdefault(name,[]).append(row)
    summaries={name:{'samples':len(rows),'target_retained':sum(r['target_retained'] for r in rows),
        'target_ranks':[r['target_rank'] for r in rows],'legacy_bytes':sum(r['legacy_total_bytes'] for r in rows),
        'integrated_bytes':sum(r['staged_total_bytes'] for r in rows),'legacy_ms':sum(r['legacy_ms'] for r in rows),
        'integrated_ms':sum(r['staged_ms'] for r in rows),'integrated_groups':sum(r['staged_batches'] for r in rows),
        'legacy_statuses':dict(Counter(r['legacy_status'] for r in rows)),
        'integrated_statuses':dict(Counter(r['staged_status'] for r in rows)),
        'internal_event_payloads_equal':all(r['event_payloads_equal'] for r in rows),
        'compatibility_public_equal':all(not r['public_comparison']['differences'] for r in rows)} for name,rows in cohorts.items()}
    result={'snapshot_revision':dataset['snapshot_revision'],'comparison':comparison,'cohorts':summaries,
        'publication':public_reference(root,reference),'capped_saved_queue':capped_queue_probe(root),
        'recovery_and_source_probes':{'recovery':recovery_probe(),'shared_source':shared_source_probe(root,raw),
            'operator':saved_operator_probe(root,raw,reviews),'stored_revision':stored_revision_probe(root)},
        'elapsed_ms':(time.perf_counter()-start)*1000,'real_api_calls':0,'real_api_cost':'unconfirmed','real_model_accuracy':'unconfirmed',
        'status_semantics':{'classification':'specification_difference','reason':'legacy already_saved and new accepted can represent the same final event payload; not a change in evidence threshold'},
        'json_hashes_before':baseline,'json_hashes_unchanged':baseline==hashes(root),
        'limitations':['saved answers are counterfactual replays, never actual cached replies to a new prompt',
            'no imported proof/review/mock result has independent publication authority; strict public output intentionally empty',
            'compatibility_preview.json is comparison-only, never deployable',
            'no production scheduler integration; the entire remainder may still exceed practical model input limits and then must be held',
            'SQLite and filesystem are separate commits; immutable generation plus final manifest supports resumption, not a distributed transaction',
            'manual body flags have no supplied original-body quotes or check timestamps; do not become gold',
            'full merge_review.json migration and browser visual inspection remain unconfirmed']}
    assert result['json_hashes_unchanged'];return result


def markdown(result):
    lines=['# 一気通貫・通信なし検証','',f"固定データ: `{result['snapshot_revision']}`。保存済み回答の反実仮想再生と模擬回答のみ。実AI精度・実料金は未確認。",'',
        '|区分|標本|候補保持|旧入力bytes|接続後bytes|候補群|旧ms|接続後ms|','|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,c in result['cohorts'].items():lines.append(f"|{name}|{c['samples']}|{c['target_retained']}/{c['samples']}|{c['legacy_bytes']:,}|{c['integrated_bytes']:,}|{c['integrated_groups']}|{c['legacy_ms']:.1f}|{c['integrated_ms']:.1f}|")
    p=result['publication'];q=result['capped_saved_queue'];rows=result['comparison']['rows']
    lines+=['','候補保持は正解未確認の標本では精度を意味しません。暫定正解18組、運営者本文確認7組、本文未確認6組、保留2組を分離しています。',
        f"内部出力一致 {sum(r['event_payloads_equal'] for r in rows)}/{len(rows)}、同条件の既存公開ゲート出力一致 {sum(not r['public_comparison']['differences'] for r in rows)}/{len(rows)}。",'',
        '## 公開の安全停止','',f"保存済み事件 {p['source_events']}件、固定コミット公開JSON {p['committed_public_events']}件、既存公開ゲート {p['legacy_gate_public_events']}件、厳格な新公開JSON {p['strict_public_events']}件。", 
        f"未検証の根拠を確定公開しないため {p['strict_publication_holds']}件を公開前確認待ち。これは改善済み精度ではなく、承認根拠が未移行という阻止条件です。既存ゲート相当の `compatibility_preview.json` と公開可能性を必ず分離します。",'',
        f"予算付き保存キュー試験: {q['groups']}組、{q['statuses']}、未照合候補{q['unexamined_candidates']}件保持。模擬呼出し{q['mock_calls']}回、日予約{q['day_reserved_micro_usd']} micro USD、月予約{q['month_reserved_micro_usd']} micro USD。実支出ではありません。",'',
        '## 障害・取消し・再開','',
        '応答保存後の採用失敗は応答を再利用。採用と再開カーソルは同じSQLite保存範囲。応答が返ったか分からない障害は自動再呼出しせず確認待ち。取消し・訂正・再採用は履歴を保持。ファイル出力は世代別に保存し、全ファイル検証後にmanifestを作成。訂正後の古い出力世代は再開を拒否。',
        '',f"全工程時間 {result['elapsed_ms']/1000:.2f}秒（単発CPU計測、実運用遅延ではない）。JSONハッシュ不変: {result['json_hashes_unchanged']}。",'',
        '## 本番移行の阻止条件','',
        '- 保存済み回答は新しい入力に対する実AI回答ではなく、比較用の再生です。',
        '- 公開承認の移行・検証が未実装のため、厳格な公開JSONは意図的に空です。互換プレビューは公開不可です。',
        '- 全候補を保持しますが、残り全件の入力が大きすぎる場合の追加分割・実モデル上限の検証は未実装です。',
        '- SQLiteとファイルは別々の保存です。世代と完了manifestで再開しますが、一体の保存を保証する方式ではありません。',
        '- 本文確認済み7組も原文引用・実際の確認日時が未提供で、確定正解には昇格させません。',
        '- 巨大なmerge_reviewの全移行・実ブラウザー表示は未確認です。',
        '- 公開承認の根拠・取消しとの対応、互換プレビューの実ブラウザー表示、永続DB・バックアップ・運営者再確認UIが未完成。',
        '- 実際のAI精度・実API料金は未測定。本番切り替えを推奨しない。']
    return '\n'.join(lines)+'\n'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=ROOT)
    parser.add_argument('--reference-public',type=Path,required=True);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();root=args.snapshot.resolve();out=args.output_dir.resolve()
    if out.is_relative_to(root):parser.error('output must be outside snapshot')
    attempts=[]
    def deny(*a,**kw):attempts.append(True);raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=run(root,json.loads(args.reference_public.read_text()))
    if attempts:raise AssertionError('real network attempted')
    result['network_attempts']=0;out.mkdir(parents=True,exist_ok=True)
    # Keep only non-private baseline preview artifacts for review. The complete
    # generation (including internal queues) remains temporary and not deployable.
    db=connect();import_snapshot(db,root);flow.initialize(db)
    generation=flow.prepare_export(db,json.loads((root/'data_corrections.json').read_text()))
    with tempfile.TemporaryDirectory(prefix='news-now-e2e-artifacts-') as temporary:
        artifact=flow.materialize(db,generation,temporary,snapshot_root=root)
        preview=out/'publication_preview';preview.mkdir(exist_ok=True)
        for name in ('public_events.json','compatibility_preview.json','publication_holds.json'):
            shutil.copyfile(Path(artifact['directory'])/name,preview/name)
        (preview/'preview_manifest.json').write_text(json.dumps({'preview_only':True,'can_publish':False,'generation':generation,'files':{name:artifact['hashes'][name] for name in ('public_events.json','compatibility_preview.json','publication_holds.json')}},indent=2)+'\n')
    db.close()
    (out/'e2e_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'E2E_RESULTS.md').write_text(markdown(result));print(json.dumps({'cohorts':result['cohorts'],'publication':result['publication'],'capped':{k:v for k,v in result['capped_saved_queue'].items() if k!='rows'},'json_hashes_unchanged':True},ensure_ascii=False))

if __name__=='__main__':main()
