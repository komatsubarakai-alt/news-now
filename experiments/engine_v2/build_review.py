"""Generate a single offline HTML from the frozen 15 manual-review pairs."""
import argparse
import hashlib
import json
from pathlib import Path

def build(root):
    path=Path(__file__).parent/'evaluation.json';raw=path.read_bytes();dataset=json.loads(raw)
    event_map={x['event_id']:x for x in json.loads((root/'events.json').read_text())}
    results={r['sample_id']:r for r in json.loads((Path(__file__).parent/'stage_results.json').read_text())['rows']}
    rows=[]
    for source in dataset['rows']:
        if source['label']!='manual_review':continue
        row=dict(source);row.pop('saved_response',None)
        row['event_name']=event_map[row['target_event_id']]['event_name']
        # A topic match is a question for the reviewer, not new identity evidence.
        hints={
            'real-019':'アレフ札幌施設と公安調査庁の検査という対象・行為が共通しています。',
            'real-020':'札幌の大雨・冠水という地域と現象が関連しています。',
            'real-021':'札幌の闇バイト強盗と指示役2人の逮捕・再逮捕が関連して見えます。',
            'real-022':'同じ見出し・URLに厚岸と札幌の2事故が含まれています。',
            'real-023':'札幌で約500キロのゲートが倒れ3人が負傷したという具体的な記述が一致しています。',
            'real-024':'札幌の給食無償化と給食量への相談が関連しています。',
            'real-025':'札幌市のインフルエンザ流行と学校閉鎖が関連しています。',
            'real-026':'新タクシー事業の札幌圏進出と買収・ブランド開始が関連して見えます。',
            'real-027':'警察官が妻への暴行容疑で逮捕されたという記述が共通しています。',
            'real-028':'札幌MICE計画の概要公表と費用・議会審議が関連しています。',
            'real-029':'白石署・妻へのDVと妻への暴行という行為が関連して見えます。',
            'real-030':'エゾタク営業開始の見出しが一致しています。',
            'real-031':'白石署の警察官と妻への暴行という記述が関連して見えます。',
            'real-032':'東区・3階建て・2階・70代女性の負傷という記述が共通しています。',
            'real-033':'札幌市のインフル注意報と23校38クラス閉鎖の記述が共通しています。'}
        row['same_reason']=hints[row['sample_id']]+'同一性は未確認です。'+('保存済みAI回答も同一性の根拠にはしません。' if source.get('saved_response') else '')
        row['different_reason']=source['reason']
        status=results[row['sample_id']]['staged_status']
        row['current_status']='評価ラベル：要手動確認／前回の模擬再生：'+status+('（採用されても正解未確認）' if status=='accepted' else '')
        rows.append(row)
    assert len(rows)==15
    payload={'repository':'komatsubarakai-alt/news-now','dataset_sha256':hashlib.sha256(raw).hexdigest(),'snapshot_revision':dataset['snapshot_revision'],'rows':rows}
    encoded=json.dumps(payload,ensure_ascii=False).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    return Path(__file__).with_name('review.html.template').read_text().replace('__SNAPSHOT__',encoded)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    args.output.write_text(build(args.snapshot))
