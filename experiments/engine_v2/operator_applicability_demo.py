"""Entirely fictional records; never import real decisions into the public demo."""
from copy import deepcopy
import operator_applicability as a

def article(number):
    return {'url':'https://example.test/news/'+str(number),'title':'架空の記事 '+str(number),'description':'動作確認専用の架空情報です。','published':'2026-10-19T01:00:00+00:00','rss_source':{'name':'架空通信'}}

def example(number=1,count=1):
    old={'event_id':'fictional-'+str(number),'event_name':'架空の確認用事件 '+str(number),'category':'事件・事故','publication_status':'published','articles':[article(number*100+i) for i in range(count)]}
    source={'articles':[article(10000+number)]}
    d={'review_key':a.group_key(source),'source_fingerprint':a.source_hash(source),'group_hash':a.digest(source),'review_revision':'fictional-v1','action':'same_event','event_id':old['event_id'],'target_hash':a.target_hash(old),'note':'架空の確認理由。原文引用の確認ではありません。','submitted_at':'2026-10-18T01:00:00+00:00'}
    d['decision_id']=a.digest({k:d.get(k) for k in a.DECISION_FIELDS})
    return {'decision':d,'original_target':old,'current_target':deepcopy(old),'source_group':source,'constraints':{'checked':True,'basis':'架空の別事件制約と競合を照合したテスト入力'}}

def examples():
    items=[]
    for eid,(count,decisions) in enumerate(((5,12),(2,5),(15,2),(1,1)),1):
        base=example(eid,count);base['current_target']['articles'].append(article(eid*100+count))
        for j in range(decisions):
            x=deepcopy(base);x['source_group']={'articles':[article(20000+eid*100+j)]}
            d=x['decision'];d.update(review_key=a.group_key(x['source_group']),source_fingerprint=a.source_hash(x['source_group']),group_hash=a.digest(x['source_group']))
            d['decision_id']=a.digest({k:d.get(k) for k in a.DECISION_FIELDS});items.append(x)
    items.extend(example(50+i) for i in range(19))
    return items
