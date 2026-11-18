"""Offline operator-reference assessment, separate from labels and production.

Review flags attest what the operator checked. They are not article bodies or
source-bound quotations and must never be inserted into an AI evidence prompt.
"""
import argparse
import hashlib
import json
import re
import socket
import urllib.request
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from unittest.mock import patch

from stage_compare import ROOT, evaluate
from merge_safety import normalized, source_context
from compare import hashes

REPOSITORY='komatsubarakai-alt/news-now'
POLICIES={'fixed4':4,'fixed8':8,'first4_rest':(4,64)}
COHORTS={
    'operator_same_both':'運営者同一・両本文確認',
    'operator_same_partial':'運営者同一・片方本文確認',
    'operator_same_neither':'運営者同一・両本文未確認',
    'operator_hold_partial':'運営者保留・片方本文確認',
    'operator_hold_neither':'運営者保留・両本文未確認',
    'operator_hold_both':'運営者保留・両本文確認',
    'operator_different_both':'運営者別事件・両本文確認',
    'operator_different_partial':'運営者別事件・片方本文確認',
    'operator_different_neither':'運営者別事件・両本文未確認',
    'provisional_same':'従来の暫定同一',
    'provisional_different':'従来の暫定別事件',
}

def sha256(raw):return hashlib.sha256(raw).hexdigest()

def strict_json(raw):
    if len(raw)>1_000_000:raise ValueError('file too large')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('duplicate JSON property')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)

def validate_records(review_raw,dataset_raw):
    review=strict_json(review_raw);dataset=strict_json(dataset_raw)
    if not isinstance(review,dict) or review.get('schema')!='news-now-review-v1' or review.get('repository')!=REPOSITORY:
        raise ValueError('schema or repository mismatch')
    if review.get('dataset_sha256')!=sha256(dataset_raw) or review.get('snapshot_revision')!=dataset.get('snapshot_revision'):
        raise ValueError('dataset hash or snapshot mismatch')
    rows=dataset['rows'];by_id={r['sample_id']:r for r in rows}
    if len(by_id)!=len(rows):raise ValueError('duplicate evaluation sample')
    expected={r['sample_id'] for r in rows if r['label']=='manual_review'}
    records=review.get('records')
    if not isinstance(records,list) or len(records)!=len(expected):raise ValueError('incomplete review set')
    checked={}
    for row in rows:
        # Validate the stored evidence independently of the attached hash.
        raw=json.dumps([row['reference_article'],row['incoming_article']],sort_keys=True,ensure_ascii=False).encode()
        if sha256(raw)!=row['evidence_sha256']:raise ValueError('evaluation evidence hash mismatch')
    for record in records:
        if not isinstance(record,dict):raise ValueError('invalid record')
        sid=record.get('sample_id')
        if not isinstance(sid,str) or sid not in expected or sid in checked:raise ValueError('unknown or duplicate sample')
        row=by_id[sid]
        if record.get('event_id')!=row['target_event_id'] or record.get('evidence_sha256')!=row['evidence_sha256']:
            raise ValueError('event or evidence mismatch')
        if record.get('record_scope')!='review_only' or record.get('choice') not in ('same_event','different_event','hold'):
            raise ValueError('invalid review scope or choice')
        if any(type(record.get(k)) is not bool for k in ('incoming_body_checked','reference_body_checked')):
            raise ValueError('body flags must be booleans')
        if not isinstance(record.get('note'),str) or len(record['note'])>5000:raise ValueError('invalid note')
        stamp=record.get('reviewed_at')
        if not isinstance(stamp,str):raise ValueError('missing review timestamp')
        try:time=datetime.fromisoformat(stamp.replace('Z','+00:00'))
        except ValueError as exc:raise ValueError('invalid review timestamp') from exc
        if time.tzinfo is None:raise ValueError('review timestamp needs timezone')
        checked[sid]=record
    if set(checked)!=expected:raise ValueError('missing review samples')
    return dataset,checked

def cohort(row,record):
    if record is None:
        return 'provisional_same' if row['label']=='same_event' else 'provisional_different'
    count=record['incoming_body_checked']+record['reference_body_checked']
    suffix=('neither','partial','both')[count]
    relation={'same_event':'same','different_event':'different','hold':'hold'}[record['choice']]
    return 'operator_'+relation+'_'+suffix

def diagnose(row,record,stored):
    left,right=row['reference_article'],row['incoming_article']
    a,b=source_context(left),source_context(right)
    flags={k:left.get(k,'')==right.get(k,'') for k in ('url','title','description','published')}
    duplicate='not_identified'
    ratio=SequenceMatcher(None,normalized(a['title']),normalized(b['title']),autojunk=False).ratio()
    if all(flags.values()):duplicate='exact_url_revision'
    elif not flags['url'] and flags['title'] and flags['published']:duplicate='same_headline_time_different_url'
    elif not flags['url'] and ratio>=0.95:duplicate='similar_headline_different_url'
    # Notes are operator reports, never observed HTTP status or downloaded bodies.
    note=record['note'];deleted=[];missing=[]
    if 'どちらの記事も削除されていた' in note:deleted=['reference','incoming']
    else:
        if '元記事は削除されていた' in note:deleted.append('reference')
        if '元記事が削除されていた' in note:deleted.append('reference')
        if '新記事は削除されていた' in note:deleted.append('incoming')
    if '新記事の本文が一文字も表示されず' in note:missing=['incoming']
    links={article['url']:[e['event_id'] for e in stored if any(v['url']==article['url'] for v in e.get('articles',[]))]
           for article in (left,right)}
    return {'duplicate_class':duplicate,'equal_fields':flags,'normalized_headline_similarity':ratio,
            'operator_reported_deleted':deleted,'operator_reported_body_missing':missing,
            'availability_basis':'operator_note_only; no external verification',
            'body_text_supplied':False,'saved_rss_extra_description_bytes':{
                'reference':len(a['description'].encode()),'incoming':len(b['description'].encode())},
            'stored_url_event_links':links,
            'same_url_multiple_events':flags['url'] and len(set(links[left['url']]))>1,
            'collection_duplicate_confirmed':False}

def summarize(rows):
    output={}
    for name in dict.fromkeys(r['cohort'] for r in rows):
        group=[r for r in rows if r['cohort']==name];new=group[0]['policy']!='legacy'
        relevant=name=='provisional_same' or name=='operator_same_both'
        positions=[r['target_position'] for r in group]
        output[name]={'samples':len(group),'target_present':sum(r['target_present'] for r in group),
            'candidate_recall':sum(r['target_present'] for r in group)/len(group) if relevant else None,
            'candidate_recall_basis':'provisional headline label' if name=='provisional_same' else
                'operator attests both bodies read; bodies/quotes not supplied' if relevant else 'not a verified positive denominator',
            'position_kind':'rank' if new else 'stored presentation order, not relevance ranking',
            'target_positions':positions,'total_input_bytes':sum(r['total_input_bytes'] for r in group),
            'batches':sum(r['batches'] for r in group),'cpu_ms':sum(r['cpu_ms'] for r in group),
            'statuses':dict(Counter(r['status'] for r in group)),
            'input_preserved':all(r['input_preserved'] for r in group),
            'candidate_states_preserved':all(r['candidate_states_preserved'] for r in group)}
    return output

def run(root,dataset_raw,review_raw):
    before=hashes(root);dataset,records=validate_records(review_raw,dataset_raw)
    original_labels={r['sample_id']:r['label'] for r in dataset['rows']}
    saved=json.loads((root/'events.json').read_text());eligible=[e['event_id'] for e in saved if e.get('publication_status')!='held']
    specimens=[]
    for source in dataset['rows']:
        record=records.get(source['sample_id'])
        target=next(e for e in saved if e['event_id']==source['target_event_id'])
        specimens.append({'sample_id':source['sample_id'],'name':source['name'],'event_id':source['target_event_id'],
            'incoming_url':source['incoming_article']['url'],'reference_url':source['reference_article']['url'],
            'original_label':source['label'],'evidence_sha256':source['evidence_sha256'],
            'operator_choice':record['choice'] if record else None,'cohort':cohort(source,record),
            'body_flags':{k:record[k] for k in ('incoming_body_checked','reference_body_checked')} if record else None,
            'operator_reference_only':record is not None,'gold_label_written':False,
            'target_excluded_by_existing_hold':target.get('publication_status')=='held',
            'diagnostics':diagnose(source,record,saved) if record else None})
    meta={r['sample_id']:r for r in specimens};runs={};summaries={}
    # Pass the original frozen dataset, never operator choices/notes/flags.
    # Existing saved replies are counterfactual replays, not fresh AI outputs.
    for policy,size in POLICIES.items():
        result=evaluate(root,dataset,batch_size=size);metrics=[];legacy=[]
        for row in result['rows']:
            info=meta[row['sample_id']];target=row['target_event_id']
            common={'sample_id':row['sample_id'],'cohort':info['cohort'],'event_id':target}
            metrics.append(common|{'policy':policy,'target_position':row['target_rank'],
                'target_present':row['target_retained'],'candidate_count':len(eligible),
                'total_input_bytes':row['staged_total_bytes'],'batches':row['staged_batches'],
                'cpu_ms':row['staged_ms'],'status':row['staged_status'],
                'input_preserved':row['input_preserved'],'candidate_states_preserved':row['all_candidate_states_preserved']})
            legacy.append(common|{'policy':'legacy','target_position':eligible.index(target)+1 if target in eligible else None,
                'target_present':target in eligible,'candidate_count':len(eligible),
                'total_input_bytes':row['legacy_total_bytes'],'batches':row['legacy_calls'],
                'cpu_ms':row['legacy_ms'],'status':row['legacy_status'],
                'input_preserved':row['legacy_input_preserved'],'candidate_states_preserved':row['legacy_catalog_preserved']})
        if 'legacy' not in runs:runs['legacy']=legacy;summaries['legacy']=summarize(legacy)
        runs[policy]=metrics;summaries[policy]=summarize(metrics)
    if not all(row['input_preserved'] and row['candidate_states_preserved'] for rows in runs.values() for row in rows):
        raise AssertionError('input or event catalog lost')
    baseline=runs['fixed4']
    for rows in (runs['fixed8'],runs['first4_rest']):
        if [(r['sample_id'],r['status'],r['target_present']) for r in rows]!=[(r['sample_id'],r['status'],r['target_present']) for r in baseline]:
            raise AssertionError('policy changed outcomes or candidate retention')
    if before!=hashes(root) or original_labels!={r['sample_id']:r['label'] for r in dataset['rows']}:
        raise AssertionError('snapshot or original labels mutated')
    independence={}
    for name in dict.fromkeys(row['cohort'] for row in specimens):
        group=[row for row in specimens if row['cohort']==name]
        independence[name]={'unique_target_events':len({r['event_id'] for r in group}),
            'unique_incoming_urls':len({r['incoming_url'] for r in group})}
    return {'schema':'news-now-operator-revalidation-v1','repository':REPOSITORY,
            'snapshot_revision':dataset['snapshot_revision'],'dataset_sha256':sha256(dataset_raw),
            'review_file_sha256':sha256(review_raw),'integrity':{'matched_records':len(records),'missing_records':0,
                'event_ids_matched':len(records),'evidence_hashes_recomputed_and_matched':len(records),
                'json_hashes_before':before,'json_hashes_after':hashes(root),'json_hashes_unchanged':True},
            'review_choices':dict(Counter(r['choice'] for r in records.values())),
            'cohort_counts':dict(Counter(r['cohort'] for r in specimens)),
            'cohort_independence':independence,
            'specimens':specimens,'summary':summaries,'rows':runs,
            'replay_protocol':'unchanged frozen replies/mocks; operator choices never affect prompts or mock answers',
            'real_api_calls':0,'real_ai_accuracy':'unconfirmed','real_api_cost':'unconfirmed',
            'gold_dataset_modified':False,'production_decisions_written':False,
            'limitations':['body flags are operator attestations, no body text or new quotations were supplied',
                'candidate recall of body-checked operator references is not model decision accuracy',
                'legacy positions are stored order, not scored ranks; all eligible candidates retained by both',
                'saved reply replay is counterfactual, not a cache hit for a new prompt',
                'incoming URL held out but event summaries/names and other articles remain; no temporal holdout',
                'UTF-8 prompt bytes; conservative token/real billing savings not measured',
                'single local CPU measurement excluding SQLite bootstrap; budget reservations not charged in replay',
                'legacy has no resumable candidate ledger; preservation fields refer to in-memory input/catalog',
                'operator review timestamp is not a source change timestamp; actual availability not rechecked']}

def markdown(result):
    lines=['# 運営者確認後の再検証','',
        '判断記録は検証用の参照情報として分離。元の評価ラベル・本番統合・運営者振り分け設定へ反映していません。',
        f"固定評価スナップショット: `{result['snapshot_revision']}`。事件ID・根拠ハッシュ15/15一致、根拠ハッシュは保存記事から再計算。",
        '同一事件13組、保留2組。両本文確認・同一7組、片方のみ確認・同一3組、両本文未確認・同一3組、保留（片方確認1・両方未確認1）。',
        '本文確認フラグは運営者の申告で、本文・完全一致引用は今回の添付に含まれません。未確認6組と保留2組を確定正解として数えていません。',
        f"両本文確認7組の対象事件は{result['cohort_independence']['operator_same_both']['unique_target_events']}件。警察官暴行の3組など同じ事件が繰り返され、7つの独立した事件とはみなせません。従来18組とも標本の重なりがあるため、合算して精度を計算しません。",
        '','## 15組の記録と個別分類','','|標本|判断|新/元本文確認|分類・注意点|','|---|---|---|---|']
    for row in result['specimens']:
        if not row['operator_reference_only']:continue
        diag=row['diagnostics'];flags=row['body_flags'];tags=[]
        if diag['operator_reported_deleted']:tags.append('削除の申告: '+','.join(diag['operator_reported_deleted']))
        if diag['operator_reported_body_missing']:tags.append('本文表示なしの申告: '+','.join(diag['operator_reported_body_missing']))
        if diag['duplicate_class']!='not_identified':tags.append('記事重複候補: '+diag['duplicate_class'])
        if diag['same_url_multiple_events']:tags.append('同一URLが別々の2事件に紐づく。記事一致を事件統合に使わない')
        lines.append(f"|{row['sample_id']} {row['name']}|{row['operator_choice']}|{flags['incoming_body_checked']}/{flags['reference_body_checked']}|{' / '.join(tags) or '異なる視点・段階の続報としての運営者判断。本文の証拠引用は未保存'}|")
    lines+=['','reference=元記事、incoming=新記事。削除・本文表示なしは申告の分類であり、外部取得やHTTP確認はしていません。',
        '022はURL・見出し・説明・日時が完全一致する記事が、厚岸と札幌北区の別事故に紐づいた例。重複収集されたと断定できず、記事共用と事件統合を区別する必要があります。',
        '030は同じ見出し・日時・媒体でURLが異なります。033は媒体・日時・末尾の表記が異なる近似見出しで、完全一致ではありません。どちらも転載・重複収集の可能性は未確定です。',
        '15組とも保存RSSには見出し以外の有効な説明がなく、本文確認の結果をAIの新しい根拠として使うには原文の引用が必要です。',
        '','## 同一条件の比較','','運営者の判断を模擬AIの回答へ入れず、以前と同じ保存済み回答・模擬回答を再実行しました。実AIの正答率・実API料金は未測定。',
        '現行の順位は並び順（関連度順位ではない）、新方式は検索順位です。保持率は候補に入っている率で、統合できた率ではありません。未確認組・保留組の対象保持は記述統計のみです。',
        '保留021の対象は保存データで既にpublication_status=heldのため、両方式とも自動照合候補から除外。今回の人の保留判断で解除していません。元記事・事件と確認記録は保持しています。',
        '','|区分|方式|組数|対象保持|対象位置|総入力バイト|群数|CPU ms|模擬再生の状態|','|---|---|---:|---:|---|---:|---:|---:|---|']
    for name in COHORTS:
        for policy,summary in result['summary'].items():
            group=summary.get(name)
            if group is None:continue
            lines.append(f"|{COHORTS[name]}|{policy}|{group['samples']}|{group['target_present']}/{group['samples']}|{group['target_positions']}|{group['total_input_bytes']:,}|{group['batches']}|{group['cpu_ms']:.2f}|{group['statuses']}|")
    lines+=['','## 判断と改善案','',
        '1. 記事の重複と事件の同一性を別に管理する。022は記事版を共有しても2事故との対応を保持し、事件を自動統合しない。',
        '2. URL・見出し・説明・公開日時が同じ記事版は処理済み参照を再利用し、改訂なら別版にする。既存PR #14の経路を活かし、重複検出を理由に元記事・取得履歴・対応関係を削除しない。',
        '3. 030は見出し・日時・媒体、033は近似見出しを重複候補の検索に使う。URL違いだけで新規扱いせず、見出し一致だけで転載確定・省略・統合もしない。',
        '4. 削除申告と本文表示なしを分け、取得済みメタデータ・元URL・確認日時・運営者メモを保持する。予算と取得規約を確認せず自動再取得を追加しない。',
        '5. 両本文確認7組について、判断に使った原文引用・出典・確認日時を次に収集する。人には同一と分かっても、保存入力に根拠がない場合は現行安全検証を緩めず保留する。',
        '6. 給食、流行と注意報、計画と議会などは「追跡テーマ」と個別の出来事・進展を分ける。人の同一判断だけで新しい進展・完結を確定しない。',
        '7. 警察署・年齢・発言などの別名/固有事実は、出典付きで候補検索に利用する。人物だけの一致で別事件を統合しない。',
        '8. 候補保持率は両方式とも高く、現時点では新方式の精度優位を証明していない。低順位の続報・時系列標本と日0.20ドル/月5ドルでの再開検証を追加してから採用判断する。',
        '','この再検証では本番移行不可。記事/事件の区別、本文引用の不足、独立した負例・難しい低順位続報の不足、実AI精度・費用未測定が阻止条件です。',
        '','## 再現と安全性','',
        '出力は入力スナップショット外。一時コピーで実行し、urllib・socket通信を禁止。元の評価JSON・確認記録・本番JSONの前後ハッシュを比較します。',
        '予算予約を行わない入力量比較であり、予算下で全群を1回の運転で照合できるという主張ではありません。予算・既存の根拠検証・Actionsを変更していません。']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,default=ROOT)
    p.add_argument('--records',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    root=a.snapshot.resolve();out=a.output_dir.resolve();records=a.records.resolve();dataset=Path(__file__).with_name('evaluation.json')
    if out.is_relative_to(root) or records==out or records.is_relative_to(out) or dataset.resolve().is_relative_to(out):
        p.error('output must be outside all input paths')
    dataset_raw=dataset.read_bytes();review_raw=records.read_bytes();attempts=[]
    def deny(*args,**kwargs):attempts.append(True);raise AssertionError('real networking forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=run(root,dataset_raw,review_raw)
    if attempts:raise AssertionError('network attempt detected')
    if dataset.read_bytes()!=dataset_raw or records.read_bytes()!=review_raw:raise AssertionError('input file mutated')
    result['network_attempts']=len(attempts)
    out.mkdir(parents=True,exist_ok=True)
    (out/'operator_revalidation_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'OPERATOR_REVALIDATION.md').write_text(markdown(result))
    print(json.dumps({'integrity':result['integrity']|{'json_hashes_before':'see report','json_hashes_after':'see report'},'cohort_counts':result['cohort_counts'],'summary':result['summary']},ensure_ascii=False))

if __name__=='__main__':main()
