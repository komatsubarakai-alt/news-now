"""Reproducible, offline comparison. Input is read only; SQLite is in-memory."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path
from statistics import mean, median
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent)]
from engine import connect, import_snapshot, events, rank, judge
from event_tracker import analysis_prompt, merge_incoming
from operator_review import active_selection
from pending_triage import current_groups, preflight, source_saved, group_key


def read(root,name,default):
    path=root/name
    return json.loads(path.read_text()) if path.exists() else default

def hashes(root):
    return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.json')}

def stats(values):
    return {'count':len(values),'min':min(values) if values else None,'median':median(values) if values else None,
            'mean':round(mean(values),3) if values else None,'max':max(values) if values else None}


def compare(root,batch_size=4):
    before=hashes(root);db=connect()
    start=time.perf_counter();import_snapshot(db,root);import_ms=(time.perf_counter()-start)*1000
    saved=read(root,'events.json',[]);triage=read(root,'tracking_triage.json',{}).get('items',{})
    registry=read(root,'data_corrections.json',{})
    groups=current_groups(read(root,'tracking_pending.json',[]),read(root,'news_status.json',[]))
    known={a['url']:a for e in saved for a in e.get('articles',[])}
    rows=[];skipped={}
    for g in groups:
        why=preflight(g,known,registry,triage)
        if why:skipped[why]=skipped.get(why,0)+1;continue
        eligible=[e for e in saved if e.get('publication_status')!='held']
        selection=active_selection(g,triage);context=''
        if selection:
            if selection['action']=='same_event':
                eligible=[e for e in eligible if e['event_id']==selection['event_id'] and not e.get('merged_into')]
            context='運営者の振り分け希望（同一性・進展の根拠ではない）。根拠不足ならuncertain。'+json.dumps({k:selection[k] for k in ('action','event_id')},ensure_ascii=False)
        fresh=[a for a in g['articles'] if not source_saved(a,known)]
        start=time.perf_counter();old=analysis_prompt(eligible,fresh,context,g.get('event_name',''));old_ms=(time.perf_counter()-start)*1000
        start=time.perf_counter();ranked=rank(db,g,triage);new_ms=(time.perf_counter()-start)*1000
        limit=1 if selection and selection['action']=='same_event' else batch_size
        batch=ranked[:limit]
        short=analysis_prompt([r['event'] for r in batch],fresh,context,g.get('event_name',''))
        new_prepare_ms=(time.perf_counter()-start)*1000
        full=analysis_prompt([r['event'] for r in ranked],fresh,context,g.get('event_name',''))
        operator_rank=next((r['rank'] for r in ranked if selection and r['event_id']==selection.get('event_id')),None)
        rows.append({'group_key':group_key(g),'old_candidates':len(eligible),'new_retained':len(ranked),
                     'first_batch':len(batch),'unexamined':len(ranked)-len(batch),'old_bytes':len(old.encode()),
                     'new_first_batch_bytes':len(short.encode()),'new_all_bytes':len(full.encode()),
                     'legacy_build_ms':old_ms,'rank_ms':new_ms,'new_prepare_ms':new_prepare_ms,'operator_target_rank':operator_rank,
                     'legacy_ids_retained':set(e['event_id'] for e in eligible)<=set(r['event_id'] for r in ranked)})
    replay=[]
    from datetime import datetime,timezone
    now=datetime(2026,10,14,6,tzinfo=timezone.utc)
    class MockAI:
        def __init__(self,answer):self.answer=answer
        def ask(self,prompt):return copy.deepcopy(self.answer)
    for item in read(root,'tracking_review_recent.json',{}).get('items',[]):
        diagnostics=item.get('diagnostics',{});answer=diagnostics.get('ai_report')
        if not isinstance(answer,dict):continue
        urls=set(diagnostics.get('input_urls',[]))
        g=next((g for g in groups if {a['url'] for a in g.get('articles',[])}==urls),None)
        if not g:continue
        old_events=copy.deepcopy(saved);old_state=copy.deepcopy(triage)
        with patch('event_tracker.load_corrections',return_value=registry),contextlib.redirect_stdout(io.StringIO()):
            merge_incoming(old_events,[copy.deepcopy(g)],MockAI(answer),now,triage=old_state)
        temp=connect();import_snapshot(temp,root)
        new_status=judge(temp,copy.deepcopy(g),answer,now,triage=triage,registry=registry)
        replay.append({'group_key':group_key(g),'new_status':new_status,'event_payloads_equal':events(temp)==old_events})
        temp.close()
    event_roundtrip=events(db)==saved
    input_url_set={a['url'] for e in saved for a in e.get('articles',[])}
    db_urls={r[0] for r in db.execute('SELECT url FROM articles')}
    imported_raw=all(db.execute('SELECT payload FROM source_files WHERE path=?',(p,)).fetchone()[0]==(root/p).read_text()
                     for (p,) in db.execute('SELECT path FROM source_files'))
    foreign_errors=list(db.execute('PRAGMA foreign_key_check'))
    unchanged=before==hashes(root)
    metrics={key:stats([r[key] for r in rows]) for key in ('old_candidates','new_retained','first_batch','unexamined',
                          'old_bytes','new_first_batch_bytes','new_all_bytes','legacy_build_ms','rank_ms','new_prepare_ms')}
    result={'real_api_calls':0,'event_count':len(saved),'input_groups':len(groups),'measured_groups':len(rows),
            'preflight_skips':skipped,'batch_size':batch_size,'metrics':metrics,
            'correct_followup_recall':'unconfirmed_no_gold_labels',
            'legacy_candidate_set_retained':all(r['legacy_ids_retained'] for r in rows),
            'operator_target_ranks':[r['operator_target_rank'] for r in rows if r['operator_target_rank'] is not None],
            'event_payload_roundtrip':event_roundtrip,'event_urls_preserved':input_url_set<=db_urls,
            'source_payload_roundtrip':imported_raw,'foreign_key_errors':foreign_errors,'input_hashes_unchanged':unchanged,
            'import_ms':round(import_ms,3),'mock_replay':replay,'token_estimate':None,
            'limitations':['first batch is not a complete decision or measured API savings',
                           'rank timing includes SQL persistence; legacy timing is prompt construction only',
                           'case/entity extraction, chronological indexes and real AI client not implemented',
                           'large merge_review archive is not imported; source Git blob is preserved'],
            'rows':rows}
    db.close()
    if not all([event_roundtrip,input_url_set<=db_urls,imported_raw,unchanged,not foreign_errors]):
        raise AssertionError('snapshot integrity failed')
    return result


def synthetic_rank_benchmark():
    """Explicit artificial labels, not inferred labels for production articles."""
    import tempfile
    from datetime import timedelta
    sys.path.insert(0,str(ROOT/'tests'))
    from test_tracking import NOW,article,event
    from test_merge_safety import group
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)
        target=event();others=[]
        for i,title in enumerate(['札幌の住宅火災','別の被害者の住宅強盗','MICE施設の建設','政策の施行',
                                  '学校給食の問題','会社の開業','別地域の道路復旧']):
            e=copy.deepcopy(target);e['event_id']='synthetic-'+str(i);e['event_name']=title
            e['articles']=[{'url':'https://example.test/other'+str(i),'title':title,'published':NOW.isoformat()}]
            others.append(e)
        (path/'events.json').write_text(json.dumps(others+[target]))
        db=connect();import_snapshot(db,path)
        output=[]
        for name,delay,category,title in [('起訴180日後',180,'事件・事故','容疑者を起訴'),
            ('判決730日後',730,'事件・事故','判決'),('地域記載なし',1,'事件・事故','被害者山田の続報'),
            ('カテゴリ変更',1,'企業・組織','容疑者を起訴'),('通常の続報',1,'事件・事故','容疑者を起訴')]:
            a=article(title,'https://example.test/'+name,(NOW+timedelta(days=delay)).isoformat())
            if name=='地域記載なし':a['title']='豊幌住宅強盗 被害者山田の続報'
            g=group(a);g['category']=category
            ranked=rank(db,g)
            position=next(r['rank'] for r in ranked if r['event_id']==target['event_id'])
            output.append({'label':name,'expected_event_id':'e1','legacy_rank':8,'new_rank':position,
                           'all_candidates_retained':len(ranked)==8})
        db.close()
        return {'provenance':'artificial_fixtures_only','cases':output,
                'all_candidate_retention':sum(x['all_candidates_retained'] for x in output)/len(output),
                'top4_retention':sum(x['new_rank']<=4 for x in output)/len(output)}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=ROOT)
    parser.add_argument('--batch-size',type=int,default=4);parser.add_argument('--output',type=Path)
    parser.add_argument('--snapshot-revision',default='unconfirmed')
    args=parser.parse_args()
    if args.batch_size<1:parser.error('batch size must be positive')
    root=args.snapshot.resolve()
    if args.output and args.output.resolve().is_relative_to(root):parser.error('output must be outside snapshot')
    def deny(*a,**kw):raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=compare(root,args.batch_size)
        result['synthetic_rank']=synthetic_rank_benchmark()
        result['snapshot_revision']=args.snapshot_revision
    text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text+'\n')
    else:print(text)


if __name__=='__main__':main()
