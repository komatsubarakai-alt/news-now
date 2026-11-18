"""Evidence-scoped display notes, never an identity classifier or gold labels."""
import json
import revision_sync as sync

FIELDS={'case_name':'事件名','place':'発生場所','occurred_at':'発生日時','entities':'関係人物・組織','action':'出来事の内容'}
REASONS={
 'different_event':['発生場所が異なる','発生日時が異なる','関係人物が異なる','出来事が異なる','複数事件を扱う記事','その他'],
 'hold':['本文未確認','引用根拠が不足','場所・時期が不明','情報に矛盾がある','追加調査が必要','その他'],
 'none':['既報と同じ内容','新しい事実がない','解説・振り返り記事','既に記録済みの進展','その他'],
 'new':['新しい事実を引用で確認','以前の記録との差を確認','重複か追加確認が必要','その他'],
 'possible_correction':['以前の報道との食い違い','訂正・撤回を示す記述','対象の情報を追加確認','その他'],
 'same_event':['事件を特定する記述を確認','追加調査が必要','その他'],
}

def reason_options(relation,progress):
    return REASONS.get(relation if relation!='same_event' else progress,REASONS['hold'] if progress=='unknown' else REASONS['same_event'])

def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS ui_comparison_notes(event_id TEXT NOT NULL,url TEXT NOT NULL,field TEXT NOT NULL,value TEXT NOT NULL,evidence_id TEXT NOT NULL REFERENCES sync_evidence,PRIMARY KEY(event_id,url,field,value,evidence_id))')

def add_note(db,event_id,url,field,value,evidence_id):
    """Explicit annotation only. Must be literal and scoped to this event + URL."""
    if field not in FIELDS or not isinstance(value,str) or not value.strip():raise ValueError('比較項目を確認してください。')
    row=db.execute('SELECT event_id,url,quote FROM sync_evidence WHERE evidence_id=?',(evidence_id,)).fetchone()
    if not row or row[:2]!=(event_id,url) or not row[2] or value not in row[2] or sync.verify_quote(db,evidence_id,current=True)['status']!='exact_literal_match':raise ValueError('この事件・記事版の引用が必要です。')
    db.execute('INSERT OR IGNORE INTO ui_comparison_notes VALUES(?,?,?,?,?)',(event_id,url,field,value,evidence_id))

def comparison(db,event,incoming_url):
    eid=event['event_id'];urls={a['url'] for a in event.get('articles',[]) if a['url']!=incoming_url}
    sides={'existing':{k:[] for k in FIELDS},'incoming':{k:[] for k in FIELDS}}
    for url,field,value,proof in db.execute('SELECT url,field,value,evidence_id FROM ui_comparison_notes WHERE event_id=? ORDER BY url,field,value,evidence_id',(eid,)):
        if field not in FIELDS or url!=incoming_url and url not in urls:continue
        row=db.execute('SELECT quote FROM sync_evidence WHERE evidence_id=?',(proof,)).fetchone()
        if not row or not row[0] or value not in row[0] or sync.verify_quote(db,proof,current=True)['status']!='exact_literal_match':continue
        sides['incoming' if url==incoming_url else 'existing'][field].append(dict(value=value,quote=row[0],url=url,evidence_id=proof,verification='保存内容との文字列一致・出典確認は未検証'))
    rows=[]
    for field,label in FIELDS.items():
        left=sides['existing'][field];right=sides['incoming'][field]
        lv={x['value'] for x in left};rv={x['value'] for x in right}
        # Multiple statements may refer to multiple people or times; never collapse them.
        status='未確認' if len(lv)!=1 or len(rv)!=1 else '一致' if lv==rv else '相違'
        rows.append(dict(field=field,label=label,existing=left,incoming=right,status=status))
    return dict(rows=rows,scope='reference_only_not_identity_proof',existing_facts=sorted({(x['url'],x['quote']) for values in sides['existing'].values() for x in values}),incoming_facts=sorted({(x['url'],x['quote']) for values in sides['incoming'].values() for x in values}))

def review_details(command):
    codes=command.get('reason_codes',[])
    if not isinstance(codes,list) or len(codes)>8 or any(not isinstance(c,str) or c not in reason_options(command.get('relation'),command.get('progress')) for c in codes) or len(codes)!=len(set(codes)):raise ValueError('判断内容に合う理由を選んでください。')
    details=command.get('progress_details')
    if details is not None:
        if command.get('relation')!='same_event' or command.get('progress')!='new' or not isinstance(details,dict):raise ValueError('進展の確認内容を確認してください。')
        if set(details)-{'new_fact','difference','evidence_ids','duplicate_checked'}:raise ValueError('不明な進展項目があります。')
        if any(not isinstance(details.get(k,''),str) or len(details.get(k,''))>1000 for k in ('new_fact','difference')):raise ValueError('進展の記述を確認してください。')
        ids=details.get('evidence_ids',[])
        if not isinstance(ids,list) or len(ids)>20 or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):raise ValueError('進展の引用を選んでください。')
        if not isinstance(details.get('duplicate_checked',False),bool):raise ValueError('重複確認を確認してください。')
    return dict(reason_codes=codes,progress_details=details)
