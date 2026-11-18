"""Read-only applicability assessments, immutable private sidecar, offline HTML.

Candidate != approval/adoption. No production writer, client, or migration API.
"""
import argparse
import copy
import hashlib
import html
import json
import re
import sqlite3
from collections import Counter,defaultdict
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
STATES={'candidate':'有効候補','recheck':'再確認待ち','invalid':'無効候補','unknown':'判定不能'}
TARGET_FIELDS=('event_id','event_name','category','publication_status','articles')
DECISION_FIELDS=('review_key','source_fingerprint','group_hash','review_revision','action','event_id','target_hash','note')
CORE=('url','title','description','published')
JST=timezone(timedelta(hours=9))

def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def rule():return 'operator-applicability-1:'+hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
def target_hash(e):return digest({k:e.get(k) for k in TARGET_FIELDS})
def source_hash(g):
    a=[{k:v.get(k,'') for k in CORE} for v in g.get('articles',[])]
    a.sort(key=lambda v:v['url'])
    return hashlib.sha256(json.dumps(a,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def group_key(g):
    urls=sorted({a.get('url','') for a in g.get('articles',[])})
    return hashlib.sha256(json.dumps(urls or g,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
def article_ref(a):return {'url':a.get('url'),'derived_version_sha256':digest({k:a.get(k,'') for k in CORE}),'stored_version_id':a.get('article_version_id'),'body_hash':digest(a['body']) if isinstance(a.get('body'),str) else None}

def article_diff(old,new,bindings=None,event_id=None):
    left=defaultdict(list);right=defaultdict(list)
    for a in old:
        if not isinstance(a,dict) or not isinstance(a.get('url'),str) or not a['url']:raise ValueError('invalid saved article')
        left[a['url']].append(a)
    for a in new:
        if not isinstance(a,dict) or not isinstance(a.get('url'),str) or not a['url']:raise ValueError('invalid current article')
        right[a['url']].append(a)
    added=[copy.deepcopy(a) for a in new if a['url'] not in left]
    deleted=[copy.deepcopy(a) for a in old if a['url'] not in right]
    revised=[{'url':u,'before':left[u],'after':right[u]} for u in sorted(left.keys()&right.keys()) if canonical(left[u])!=canonical(right[u])]
    moved=[]
    for a in deleted:
        destinations=sorted(set((bindings or {}).get(a['url'],[]))-{event_id})
        if destinations:moved.append({'article':a,'event_ids':destinations,'basis':'caller_supplied_current_event_bindings'})
    shared_order=[a['url'] for a in old if a['url'] in right]!=[a['url'] for a in new if a['url'] in left]
    return {'added':added,'deleted':deleted,'revised':revised,'moved':moved,'shared_order_changed':shared_order,
            'multiple_versions_one_url':any(len(v)>1 for v in list(left.values())+list(right.values()))}

def assess(item):
    item=copy.deepcopy(item);d=item.get('decision');old=item.get('original_target');new=item.get('current_target');g=item.get('source_group')
    if not isinstance(d,dict) or not isinstance(d.get('decision_id'),str) or not d['decision_id']:raise ValueError('decision ID required')
    result={'decision_id':d['decision_id'],'event_id':d.get('event_id'),'state':'unknown','reasons':[],
            'warnings':['有効候補は承認・自動適用ではありません。','保存理由は原文引用の確認を意味しません。'],
            'original_decision':d,'original_target':old,'current_target':new,'source_group':g,
            'saved_hash':d.get('target_hash'),'current_hash':target_hash(new) if isinstance(new,dict) else None,
            'dependencies':copy.deepcopy(item.get('dependencies')),'can_apply':False,'can_approve':False,'can_publish':False,'rule_id':rule()}
    def finish(state,*reasons):
        result.update(state=state,reasons=list(reasons));result['assessment_id']=digest(result);return result
    if not isinstance(old,dict) or not isinstance(new,dict) or not isinstance(g,dict):return finish('unknown','判断当時・現在・元記事の保存情報が不足しています。')
    if digest({k:d.get(k) for k in DECISION_FIELDS})!=d['decision_id']:return finish('unknown','判断IDと保存内容が一致しません。生成方式・記録を確認してください。')
    if old.get('event_id')!=d.get('event_id') or target_hash(old)!=d.get('target_hash'):return finish('unknown','判断時の事件状態を保存ハッシュで証明できません。')
    result['diff']=article_diff(old.get('articles',[]),new.get('articles',[]),item.get('article_event_bindings'),new.get('event_id'))
    result['changed_target_fields']=[k for k in TARGET_FIELDS if old.get(k)!=new.get(k)]
    c=item.get('constraints')
    if not isinstance(c,dict) or not c.get('checked') or not isinstance(c.get('basis'),str) or not c['basis'].strip():return finish('unknown','別事件制約・判断競合の照合範囲が記録されていません。')
    result['constraint_basis']=c['basis']
    if new.get('event_id')!=d['event_id'] or new.get('merged_into') or new.get('split_into') or c.get('merged_into') or c.get('split_into'):
        return finish('invalid','事件IDの変更・分割・統合があります。元の判断を別事件へ適用しないでください。')
    if c.get('conflict') or c.get('known_distinct') or new.get('publication_status')=='held' or new.get('category')=='対象外':
        return finish('invalid','判断競合・別事件制約、または対象事件の利用停止があります。')
    if result['diff']['moved']:return finish('invalid','元記事が別事件へ移動した対応があります。引き継ぎを止めて確認してください。')
    if group_key(g)!=d.get('review_key') or source_hash(g)!=d.get('source_fingerprint') or digest(g)!=d.get('group_hash'):
        return finish('recheck','判断の対象だった記事版・記事グループが変わっています。')
    if not isinstance(d.get('note'),str) or not d['note'].strip():return finish('unknown','保存された判断理由がありません。')
    dep=item.get('dependencies')
    if dep is None or dep == []:
        result['warnings'].append('引用・本文版の依存関係は未記録です。記録の参照候補であり、根拠成立は未確認です。')
    elif not isinstance(dep,list) or any(not isinstance(x,dict) for x in dep):return finish('unknown','根拠依存記録の形式が不明です。')
    elif any(x.get('state') in ('changed','expired','cancelled','needs_recheck') for x in dep):return finish('recheck','依存する記事版・引用・判断が変更または失効しています。')
    elif any(x.get('state')!='current' for x in dep):return finish('unknown','根拠の現在状態が確認できません。')
    if result['diff']['multiple_versions_one_url']:return finish('recheck','同じURLに複数の記事版があります。代表化と記事改訂を区別してください。')
    if result['changed_target_fields'] or old.get('summary')!=new.get('summary') or old.get('current_stage')!=new.get('current_stage'):
        reasons=[];diff=result['diff']
        if diff['added']:reasons.append('記事が追加され、判断時の事件構成と変わっています。追加記事の影響を確認してください。')
        if diff['revised']:reasons.append('保存記事が改訂されています。元の引用と判断への影響を確認してください。')
        if diff['deleted']:reasons.append('判断時の記事が現在の構成から外れています。削除と移動を確認してください。')
        if diff['shared_order_changed']:reasons.append('記事の並び順が変わっています。ハッシュ差と意味上の変更を区別してください。')
        if not reasons:reasons.append('事件名・分類・公開状態・概要・進展段階などの内容が変わっています。')
        if dep is None:reasons.append('依存関係が未記録のため、記事追加だけでも影響なしとは確定できません。')
        return finish('recheck',*reasons)
    return finish('candidate','対象事件・元記事版・保存理由が対応する参照用の有効候補です。自動適用は許可されません。')

def evaluate(items):
    unique={}
    for item in items:
        did=item.get('decision',{}).get('decision_id')
        if not did:raise ValueError('decision ID required')
        if did in unique and canonical(unique[did])!=canonical(item):raise ValueError('conflicting duplicate decision ID')
        unique[did]=item
    rows=[assess(x) for x in unique.values()]
    return {'schema':1,'rule_id':rule(),'assessments':rows,'counts':{k:sum(r['state']==k for r in rows) for k in STATES},'read_only':True,'can_apply':False}

@contextmanager
def transaction(db):
    db.execute('SAVEPOINT applicability')
    try:yield
    except BaseException:
        db.execute('ROLLBACK TO applicability');db.execute('RELEASE applicability');raise
    else:db.execute('RELEASE applicability')

def open_store(path=':memory:'):
    if path!=':memory:':
        p=Path(path)
        if p.is_symlink() or p.resolve().is_relative_to(ROOT) or p.suffix not in ('.sqlite','.sqlite3','.db'):raise ValueError('private sidecar outside repository required')
        if p.exists() and p.stat().st_size:
            check=sqlite3.connect('file:'+str(p.resolve())+'?mode=ro',uri=True)
            try:known=check.execute("SELECT 1 FROM sqlite_master WHERE name='applicability_meta'").fetchone()
            finally:check.close()
            if not known:raise ValueError('not an applicability sidecar; existing DB is untouched')
    db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE IF NOT EXISTS applicability_meta(version INTEGER PRIMARY KEY);
    INSERT OR IGNORE INTO applicability_meta VALUES(1);
    CREATE TABLE IF NOT EXISTS original_decisions(decision_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS applicability_assessments(assessment_id TEXT PRIMARY KEY,decision_id TEXT NOT NULL,payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS applicability_runs(operation_id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,result TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS originals_no_update BEFORE UPDATE ON original_decisions BEGIN SELECT RAISE(ABORT,'immutable original'); END;
    CREATE TRIGGER IF NOT EXISTS originals_no_delete BEFORE DELETE ON original_decisions BEGIN SELECT RAISE(ABORT,'immutable original'); END;
    CREATE TRIGGER IF NOT EXISTS assessments_no_update BEFORE UPDATE ON applicability_assessments BEGIN SELECT RAISE(ABORT,'immutable assessment'); END;
    CREATE TRIGGER IF NOT EXISTS assessments_no_delete BEFORE DELETE ON applicability_assessments BEGIN SELECT RAISE(ABORT,'immutable assessment'); END;''')
    return db

def record(db,operation_id,items,at,*,fail_save=False):
    if not isinstance(operation_id,str) or not operation_id.strip() or not isinstance(at,datetime) or at.tzinfo is None:raise ValueError('operation and timezone-aware timestamp required')
    ih=digest([items,rule()])
    with transaction(db):
        old=db.execute('SELECT input_hash,result FROM applicability_runs WHERE operation_id=?',(operation_id,)).fetchone()
        if old:
            if old[0]!=ih:raise ValueError('operation reused with changed inputs')
            return json.loads(old[1])
        result=evaluate(items)
        for row in result['assessments']:
            d=row['original_decision'];did=d['decision_id'];prior=db.execute('SELECT payload_hash FROM original_decisions WHERE decision_id=?',(did,)).fetchone()
            if prior and prior[0]!=digest(d):raise ValueError('original decision changed; no overwrite allowed')
            db.execute('INSERT OR IGNORE INTO original_decisions VALUES(?,?,?)',(did,digest(d),canonical(d)))
            db.execute('INSERT OR IGNORE INTO applicability_assessments VALUES(?,?,?)',(row['assessment_id'],did,canonical(row)))
        if fail_save:raise RuntimeError('simulated sidecar failure')
        db.execute('INSERT INTO applicability_runs VALUES(?,?,?,?)',(operation_id,ih,at.isoformat(),canonical(result)))
        return result

def restore_originals(db):return [json.loads(r[0]) for r in db.execute('SELECT payload FROM original_decisions ORDER BY decision_id')]

def japanese_date(value):
    if not isinstance(value,str):return '未確認'
    try:
        from email.utils import parsedate_to_datetime
        try:d=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:d=parsedate_to_datetime(value)
        if not d or d.tzinfo is None:return value+'（時差未確認）'
        return d.astimezone(JST).strftime('%Y年%m月%d日 %H:%M')+'（日本時間）'
    except (ValueError,TypeError,OverflowError):return value+'（日時未確認）'

def render(result):
    esc=lambda s:html.escape(str(s if s is not None else '未記録'))
    def article(a):
        src=a.get('rss_source');name=src.get('name') if isinstance(src,dict) else None
        return '<li><strong>'+esc(a.get('title','見出し未記録'))+'</strong><p>'+esc(name or '配信元未確認')+' ／ '+esc(japanese_date(a.get('published')))+'</p><details><summary>元URL・記事版の詳細</summary><p>'+esc(a.get('url'))+'</p><code>'+esc(canonical(article_ref(a)))+'</code><p>本文・出典の確認状態：未確認</p></details></li>'
    def listing(articles):return '<ul>'+''.join(article(a) for a in articles)+'</ul>' if articles else '<p>該当なし</p>'
    grouped=defaultdict(list)
    for r in result['assessments']:grouped[(r.get('event_id'),r.get('saved_hash'),r.get('current_hash'))].append(r)
    ordered=sorted(grouped.values(),key=lambda rs:(all(r['state']=='candidate' for r in rs),rs[0].get('event_id') or ''))
    cards=[];links=[]
    for i,rows in enumerate(ordered):
        r=rows[0];old=r.get('original_target') or {};new=r.get('current_target') or {};diff=r.get('diff',{});anchor='event-'+str(i)
        title=new.get('event_name') or old.get('event_name') or '事件情報未確認'
        links.append('<a class="nav" href="#'+anchor+'">'+esc(title)+'：'+str(len(rows))+'判断</a>')
        card='<section id="'+anchor+'"><h2>'+esc(title)+'</h2><p class="identifier">事件ID：'+esc(r.get('event_id'))+'</p><p><strong>判断時 '+str(len(old.get('articles',[])))+'記事 → 現在 '+str(len(new.get('articles',[])))+'記事 ／ 影響 '+str(len(rows))+'判断</strong></p>'
        card+='<p>ハッシュ比較：'+('一致（自動適用の許可ではありません）' if r.get('saved_hash')==r.get('current_hash') else '不一致（更新理由を確認）')+'</p>'
        for kind,label in [('added','追加'),('revised','改訂'),('deleted','削除・構成から除外'),('moved','別事件への移動')]:
            value=diff.get(kind,[])
            if value:
                card+='<div class="change '+kind+'"><h3>'+label+'：'+str(len(value))+'件</h3>'
                if kind in ('added','deleted'):card+=listing(value)
                elif kind=='revised':
                    for v in value:card+='<p>改訂前</p>'+listing(v['before'])+'<p>改訂後</p>'+listing(v['after'])
                else:
                    for v in value:card+=listing([v['article']])+'<p>移動先事件ID：'+esc(', '.join(v['event_ids']))+'</p>'
                card+='</div>'
        card+='<p>変更理由：'+esc('記事構成に変更なし' if not r.get('changed_target_fields') else '・'.join({'articles':'記事構成','event_name':'事件名','category':'分類','publication_status':'公開状態','event_id':'事件ID'}.get(k,k) for k in r['changed_target_fields'])+'が変更')+'</p>'
        card+='<details><summary>判断時点の記事一覧（'+str(len(old.get('articles',[])))+'件）</summary>'+listing(old.get('articles',[]))+'</details>'
        card+='<details><summary>現在の記事一覧（'+str(len(new.get('articles',[])))+'件）</summary>'+listing(new.get('articles',[]))+'</details>'
        card+='<h3>各判断への影響</h3>'
        for row in rows:
            d=row['original_decision'];state=STATES[row['state']]
            card+='<details class="judgment"><summary>'+esc(state)+' ／ 判断 '+esc(d['decision_id'][:12])+'</summary><p>現在への適用評価：<strong>'+esc(state)+'</strong></p><p>元の判断：'+esc({'same_event':'同じ事件','new_event':'新しい事件','exclude':'対象外'}.get(d.get('action'),'未確認'))+'</p><p>保存理由（原文引用ではありません）：'+esc(d.get('note'))+'</p><p>判断日時：'+esc(japanese_date(d.get('submitted_at')))+'</p><ul>'+''.join('<li>'+esc(x)+'</li>' for x in row['reasons']+row['warnings'])+'</ul><p>引用根拠・依存関係：'+esc('未記録・未検証' if row['dependencies'] is None else canonical(row['dependencies']))+'</p><details><summary>判断ID・ハッシュ・照合の詳細</summary><pre>'+esc(canonical({k:row.get(k) for k in ['decision_id','assessment_id','saved_hash','current_hash','changed_target_fields','constraint_basis','rule_id']}))+'</pre></details><details><summary>この判断の元記事・記事版</summary>'+listing((row.get('source_group') or {}).get('articles',[]))+'</details></details>'
        cards.append(card+'<a class="nav" href="#top">一覧へ戻る</a></section>')
    counts=' ／ '.join(STATES[k]+' '+str(result['counts'][k])+'件' for k in STATES)
    return '''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>運営者判断の差分確認（閲覧専用）</title><style>body{font:16px/1.6 sans-serif;max-width:900px;margin:auto;padding:12px;padding-bottom:50px;overflow-wrap:anywhere}h1{font-size:23px}h2{font-size:21px}h3{font-size:18px}section{border:1px solid #888;border-radius:12px;padding:12px;margin:18px 0;scroll-margin:12px}.nav,summary{display:block;min-height:44px;padding:8px;box-sizing:border-box}summary{cursor:pointer;font-weight:bold}.nav{border:1px solid #bbb;border-radius:8px;margin:6px 0}.identifier,code{font-size:12px}details{margin:8px 0}ul{padding-left:20px}li{margin-bottom:12px}.change{border-left:5px solid #666;padding-left:10px}.added{border-color:#168044}.revised{border-color:#ad6500}.deleted,.moved{border-color:#a52828}.judgment{border-top:1px solid #bbb;padding-top:5px}pre{white-space:pre-wrap;font-size:12px}p{margin:8px 0}*{max-width:100%;box-sizing:border-box}</style></head><body id="top"><h1>運営者判断の差分確認</h1><p><strong>閲覧専用・非公開ローカル用</strong>：判断変更・承認・保存・本番反映はできません。</p><p>有効候補は自動承認ではありません。追加記事の同一性・矛盾・元の根拠への影響を確認してください。</p><p>'''+esc(counts)+'</p><nav>'+''.join(links)+'</nav>'+''.join(cards)+'</body></html>'

def write_html(result,output):
    output=Path(output)
    if output.is_symlink() or output.resolve().is_relative_to(ROOT):raise ValueError('HTML must be generated outside repository')
    output.write_text(render(result),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--input');group.add_argument('--demo',action='store_true');parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.demo:
        from operator_applicability_demo import examples
        items=examples()
    else:items=json.loads(Path(args.input).read_text())['items']
    result=evaluate(items);write_html(result,args.output)
    print(canonical({'counts':result['counts'],'read_only':True}))
