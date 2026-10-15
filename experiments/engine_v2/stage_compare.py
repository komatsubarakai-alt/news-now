"""Source-labeled retrospective replay. No real AI or news access."""
import argparse
import contextlib
import copy
import io
import json
import socket
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent)]
from evaluation import build,markdown
from engine import connect,import_snapshot,events,dumps,project_event
from staged import begin,step,inspect
from event_tracker import merge_incoming,TRACKING_RESPONSE
from merge_safety import source_context
from pending_triage import group_key
from compare import hashes,stats

NOW=datetime(2026,10,14,14,tzinfo=timezone.utc)


def holdout(saved,sample):
    """Same held-out corpus for both systems; no production file changes."""
    output=copy.deepcopy(saved);url=sample['incoming_article']['url']
    for event in output:
        event['articles']=[a for a in event.get('articles',[]) if a['url']!=url]
    return output


def answer_for(sample,batch):
    """Label-driven mock, not an estimate of model accuracy."""
    answer=copy.deepcopy(TRACKING_RESPONSE)
    target=sample['target_event_id'];incoming=sample['incoming_article'];reference=sample['reference_article']
    if target not in batch:return answer
    if sample.get('saved_response'):return copy.deepcopy(sample['saved_response'])
    if sample['label']=='manual_review':return answer
    answer.update(relation='same_event',event_id=target,confidence='high',evidence_urls=[incoming['url']])
    anchors=sample['anchors'] if sample['label']=='same_event' else [{'kind':'case','value':'札幌'},{'kind':'detail','value':'事件'}]
    answer['identity_matches']=[{'incoming_url':incoming['url'],'existing_url':reference['url'],'anchors':[
        {**a,'existing_quote':sample['reference_quote'],'incoming_quote':sample['incoming_quote']} for a in anchors]}]
    # Negative mocks deliberately propose the wrong target, testing unchanged guards.
    return answer


def evaluate(root,dataset,batch_size=4,db_hook=None):
    before=hashes(root);saved=json.loads((root/'events.json').read_text())
    triage=json.loads((root/'tracking_triage.json').read_text()).get('items',{})
    registry=json.loads((root/'data_corrections.json').read_text())
    event_map={e['event_id']:e for e in saved};results=[]
    for sample in dataset['rows']:
        incoming=sample['incoming_article']
        # Fixed valid category isolates identity adjudication; not category accuracy.
        group={'event_name':source_context(incoming)['title'],'category':event_map[sample['target_event_id']].get('category','事件・事故'),
               'tracking_value':'high','articles':[copy.deepcopy(incoming)]}
        corpus=holdout(saved,sample)
        eligible=[e for e in corpus if e.get('publication_status')!='held']
        old=copy.deepcopy(corpus);old_state=copy.deepcopy(triage)
        old_prompts=[]
        class LegacyMock:
            def ask(self,prompt):
                old_prompts.append(prompt);return answer_for(sample,[e['event_id'] for e in eligible])
        start=time.perf_counter()
        with patch('event_tracker.load_corrections',return_value=registry),contextlib.redirect_stdout(io.StringIO()):
            waiting=merge_incoming(old,[copy.deepcopy(group)],LegacyMock(),NOW,triage=old_state)
        old_ms=(time.perf_counter()-start)*1000
        old_status=old_state.get(group_key(group),{}).get('status','unknown')
        # Manual review leaves the automatic queue but remains in triage.group.
        old_groups=waiting+[record.get('group',{}) for record in old_state.values()]
        old_input_preserved=any(incoming==article for e in old for article in e.get('articles',[])) or any(
            incoming==article for pending in old_groups for article in pending.get('articles',[]))
        old_catalog_preserved={e['event_id'] for e in corpus}<={e['event_id'] for e in old}
        db=connect();start=time.perf_counter();import_snapshot(db,root)
        if db_hook is not None:db_hook(db)
        with db:
            for e in corpus:
                db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),e['event_id']))
                project_event(db,e,'holdout_view_only')
        bootstrap_ms=(time.perf_counter()-start)*1000
        start=time.perf_counter();sid=begin(db,group,triage,registry,batch_size=batch_size)
        state=inspect(db,sid);rank_position=next((i+1 for i,eid in enumerate(state['plan']) if eid==sample['target_event_id']),None)
        calls=[]
        def new_mock(prompt,batch):
            calls.append(prompt);return answer_for(sample,batch)
        for _ in range(40):
            if state['status']!='ready':break
            state=step(db,sid,new_mock,NOW,triage,registry)
        else:raise AssertionError('unbounded scheduler')
        new_ms=(time.perf_counter()-start)*1000
        result_events=events(db)
        changed_new={e['event_id'] for e in result_events if e!=next(x for x in corpus if x['event_id']==e['event_id'])}
        changed_old={e['event_id'] for e in old if e!=next(x for x in corpus if x['event_id']==e['event_id'])}
        if sample['label']=='different_event':
            new_wrong=sample['target_event_id'] in changed_new;old_wrong=sample['target_event_id'] in changed_old
        elif sample['label']=='same_event':
            new_wrong=bool(changed_new-{sample['target_event_id']});old_wrong=bool(changed_old-{sample['target_event_id']})
        else:
            new_wrong=old_wrong=None
        query_retained=json.loads(db.execute('SELECT group_payload FROM sessions WHERE session_id=?',(sid,)).fetchone()[0])==group
        retained_states=len(state['candidate_states'])==len(eligible)
        results.append({'sample_id':sample['sample_id'],'label':sample['label'],'mock_source':'saved_response_counterfactual' if sample.get('saved_response') else 'label_driven_guard_test',
                        'target_event_id':sample['target_event_id'],'target_rank':rank_position,
                        'target_retained':sample['target_event_id'] in state['candidate_states'],
                        'legacy_status':old_status,'staged_status':state['status'],
                        'legacy_calls':len(old_prompts),'staged_batches':len(calls),
                        'legacy_total_bytes':sum(len(p.encode()) for p in old_prompts),
                        'staged_total_bytes':sum(len(p.encode()) for p in calls),
                        'legacy_ms':old_ms,'staged_ms':new_ms,'sqlite_bootstrap_ms':bootstrap_ms,
                        'legacy_wrong_merge':old_wrong,'staged_wrong_merge':new_wrong,
                        'legacy_changed_event_ids':sorted(changed_old),'staged_changed_event_ids':sorted(changed_new),
                        'event_payloads_equal':old==result_events,'input_preserved':query_retained,
                        'legacy_input_preserved':old_input_preserved,'legacy_catalog_preserved':old_catalog_preserved,
                        'all_candidate_states_preserved':retained_states,'states':dict(Counter(state['candidate_states'].values()))})
        db.close()
    summary={}
    for label in ('same_event','different_event','manual_review'):
        rows=[r for r in results if r['label']==label]
        summary[label]={'samples':len(rows),'target_retained':sum(r['target_retained'] for r in rows),
                        'ranks':stats([r['target_rank'] for r in rows if r['target_rank'] is not None]),
                        'legacy_statuses':dict(Counter(r['legacy_status'] for r in rows)),
                        'staged_statuses':dict(Counter(r['staged_status'] for r in rows)),
                        'legacy_wrong_merges':sum(bool(r['legacy_wrong_merge']) for r in rows) if label!='manual_review' else None,'staged_wrong_merges':sum(bool(r['staged_wrong_merge']) for r in rows) if label!='manual_review' else None,
                        **{key:stats([r[key] for r in rows]) for key in ('legacy_total_bytes','staged_total_bytes','legacy_ms','staged_ms','staged_batches')}}
    unchanged=before==hashes(root)
    if not unchanged or not all(r['input_preserved'] and r['all_candidate_states_preserved'] for r in results):raise AssertionError('data lost')
    return {'snapshot_revision':dataset['snapshot_revision'],'label_counts':dataset['label_counts'],'summary':summary,
            'rows':results,'json_hashes_unchanged':unchanged,'real_api_calls':0,
            'independent_human_review':'not_completed','real_model_accuracy':'unconfirmed','real_api_cost':'unconfirmed',
            'limitations':['purpose-selected mostly reprint positives; labels verified only against stored source text',
                'incoming URL withheld from all event current articles but names/summaries/reprints remain; not a temporal holdout',
                'mock answers are label-driven; saved answers are counterfactual injections, not new-prompt cache hits',
                'CPU measured once, sqlite bootstrap reported separately; no total-production latency claim',
                'negative pair labels do not label every other event; other candidates remain unresolved']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--snapshot',type=Path,default=ROOT)
    parser.add_argument('--snapshot-revision',required=True);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();root=args.snapshot.resolve();out=args.output_dir.resolve()
    if out.is_relative_to(root):parser.error('output must be outside snapshot')
    def deny(*a,**kw):raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        data=build(root,args.snapshot_revision);result=evaluate(root,data)
    out.mkdir(parents=True,exist_ok=True)
    (out/'evaluation.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    (out/'EVALUATION.md').write_text(markdown(data))
    (out/'stage_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'labels':data['label_counts'],'summary':result['summary'],'hashes_unchanged':result['json_hashes_unchanged']},ensure_ascii=False))


if __name__=='__main__':main()
