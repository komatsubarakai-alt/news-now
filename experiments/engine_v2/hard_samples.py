"""Candidate discovery in saved source text; never auto-label as ground truth."""
import argparse
import json
from pathlib import Path

FILES=('events.json','tracking_pending.json','tracking_triage.json','news_status.json','status_pending.json','operator_review_decisions.json')
def discover(root):
    versions={};urls={}
    def visit(value,origin):
        if isinstance(value,dict):
            if isinstance(value.get('url'),str) and isinstance(value.get('title'),str):
                key=tuple(str(value.get(k,'')) for k in ('url','title','description','published'))
                versions.setdefault(key,{'article':value,'origins':[]})['origins'].append(origin)
                urls.setdefault(value['url'],set()).add(key)
            for k,v in value.items():visit(v,origin+'/'+k)
        elif isinstance(value,list):
            for i,v in enumerate(value):visit(v,origin+'/'+str(i))
    for name in FILES:visit(json.loads((root/name).read_text()),name)
    data=json.loads(Path(__file__).with_name('evaluation.json').read_text())
    rowmap={r['sample_id']:r for r in data['rows']}
    cases=[]
    def pair(sid,features,reason):
        source=rowmap[sid]
        cases.append({'sample_id':'hard-'+sid,'features':features,'kind':'saved_real_articles','label':'manual_review',
                      'event_id':source['target_event_id'],'articles':[source['reference_article'],source['incoming_article']],
                      'quotes':[source['reference_quote'],source['incoming_quote']],'reason':reason,'body_verified':False})
    long=next(v for v in versions.values() if '10年前の未解決事件' in v['article']['title'] and '起訴' in v['article']['title'])
    cases.append({'sample_id':'hard-long-prosecution','features':['長期間後の起訴','同一人物の別事件'],
                  'kind':'saved_real_article_single','label':'manual_review','event_id':None,
                  'articles':[long['article']],'origins':long['origins'],'quotes':[long['article']['title']],
                  'reason':'見出しに10年前の札幌事件の起訴と今年の小樽での別事件を明記。元の10年前の報道との対は未発見。個別事件を人物だけで統合しない。','body_verified':False})
    pair('real-026',['事件名が消えた見出し'],'元記事には「ニューモ」、新記事には「エゾタク」。ブランド・買収の対象範囲は未確認。事件名がない続報と断定しない。')
    tool=next(v for v in versions.values() if '工具に別の人' in v['article']['title'] and '札幌' not in v['article']['title'])
    events=json.loads((root/'events.json').read_text())
    target=next(e for e in events if any('倉庫' in a['title'] and '30歳' in a['title'] for a in e.get('articles',[])))
    ref=next(a for a in target['articles'] if '倉庫' in a['title'] and '30歳' in a['title'])
    cases.append({'sample_id':'hard-no-region','features':['地域名が省略'],
                  'kind':'saved_real_articles','label':'manual_review','event_id':target['event_id'],
                  'articles':[ref,tool['article']],'origins':tool['origins'],'quotes':[ref['title'],tool['article']['title']],
                  'reason':'地域のない記事と札幌の工具窃盗で年齢・倉庫・工具が共通。同一事件かは本文等で要確認。','body_verified':False})
    pair('real-011',['似た事件名の別事件'],'厚別区大谷地と東区の火災を比較。保存見出し上の別事件暫定標本だが、本文と人の独立確認は未完了。')
    changed=next(ids for ids in urls.values() if len(ids)>1 and len({k[3] for k in ids})>1)
    selected=[versions[k] for k in sorted(changed)]
    cases.append({'sample_id':'hard-revision','features':['同URLの改訂候補'],
                  'kind':'saved_real_article_versions','label':'manual_review',
                  'event_id':next((e['event_id'] for e in events if any(a['url']==selected[0]['article']['url'] for a in e.get('articles',[]))),None),
                  'articles':[v['article'] for v in selected],'origins':[v['origins'] for v in selected],
                  'quotes':[v['article']['title'] for v in selected],
                  'reason':'同URLで公開日時の差を保存データに確認。内容訂正の事実とは断定できない。','body_verified':False})
    return {'snapshot_revision':data['snapshot_revision'],'source_files':list(FILES),'unique_article_versions':len(versions),
            'unique_urls':len(urls),'multi_version_urls':sum(len(ids)>1 for ids in urls.values()),
            'saved_real_cases':len(cases),'confirmed_hard_same_event_pairs':0,
            'human_review_required':len(cases),'verified_corrections':0,'verified_long_later_verdict_pairs':0,
            'cases':cases,'synthetic_cases':'existing test_engine/test_staged fixtures; excluded from real label statistics',
            'limitations':['source-text observations only; no body or external retrieval',
                           'single indictment article is not a labeled original/follow-up pair',
                           'multi-version URL is not proof of a correction; repeated copied metadata may differ']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.write_text(json.dumps(discover(a.snapshot),ensure_ascii=False,indent=2)+'\n')
