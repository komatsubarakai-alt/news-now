"""Offline fictional demo, or a private snapshot with explicit private output."""
import argparse,json,socket,time
from pathlib import Path
from unittest.mock import patch
from .allocation import PLANS,cost_profile,opportunities,simulate,native
from .snapshot import adapt,summarize_wait


def fictional():
    jobs={'pair':[],'merge':[],'status':[]}
    for stage,count,size in [('pair',48,6100),('merge',16,5500),('status',120,2500)]:
        for i in range(count):
            item={'id':f'fictional-{stage}-{i}','stage':stage,'prompt_bytes':size,'context_signature':f'fictional-version-{i}'}
            if stage=='status':item.update(versions=[f'fictional-article-{i}'],first_seen_at='2026-01-01T00:00:00Z',headline_signal=i%9==0)
            jobs[stage].append(item)
    rows=[{'stage':s,'prompt_bytes':n,'input_tokens':n//3,'output_tokens':o} for s,n,o in [('pair',6100,550),('merge',5500,280),('status',2500,150)]]
    return jobs,[],{'days':{},'months':{}},'2026-02-01T02:43:00Z','2026-01-31T22:28:00Z',cost_profile(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--snapshot',type=Path);parser.add_argument('--profiles',type=Path);parser.add_argument('--private-output',type=Path);parser.add_argument('--wait-evidence',type=Path);args=parser.parse_args()
    if args.snapshot:
        if not args.profiles or not args.private_output:parser.error('private snapshot requires profiles and private-output')
        destination=args.private_output.resolve();repo=Path(__file__).resolve().parents[2]
        if destination.is_relative_to(repo) or destination.is_relative_to(args.snapshot.resolve()):parser.error('real-data output must stay outside repository and source snapshot')
    elif args.profiles or args.private_output or args.wait_evidence:parser.error('demo does not accept private arguments')
    with patch.object(socket.socket,'connect',side_effect=AssertionError('network disabled')),patch.object(socket,'create_connection',side_effect=AssertionError('network disabled')),patch.object(native,'response',side_effect=AssertionError('AI disabled')):
        if args.snapshot:
            profiles=cost_profile(json.loads(args.profiles.read_text())['profiles']);jobs,other,health=adapt(args.snapshot,profiles)
            seed=json.loads((args.snapshot/'ai_budget.json').read_text());asof=health['updated_at'];last=seed['last_ai_run_at']
        else:jobs,other,seed,asof,last,profiles=fictional()
        windows=opportunities(asof,last);start=time.perf_counter();results=[]
        for mode in ('estimated_settlement','reservation_only'):
            for plan in PLANS:results.append(simulate(plan,jobs,seed,windows,profiles,other_versions=other,asof=asof,cost_mode=mode))
        unknown=[simulate(p,jobs,seed,windows,profiles,other_versions=other,asof=asof,outcome='unknown') for p in PLANS]
        downstream=[simulate(p,jobs,seed,windows,profiles,other_versions=other,asof=asof,outcome='mock_downstream') for p in PLANS]
        output={'data_kind':'private_saved_snapshot' if args.snapshot else 'fictional_only','wait_reasons':summarize_wait(args.snapshot,json.loads(args.wait_evidence.read_text()) if args.wait_evidence else None) if args.snapshot else None,'elapsed_seconds':time.perf_counter()-start,'results':results,'unknown_answer_results':unknown,'downstream_transfer_results':downstream,'real_API_calls':0,'external_news_fetches':0}
    text=json.dumps(output,ensure_ascii=False,indent=2)
    if args.snapshot:args.private_output.write_text(text,encoding='utf-8');print('Private results written outside repository. No source records changed.')
    else:print(text)

if __name__=='__main__':main()
