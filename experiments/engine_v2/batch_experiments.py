"""Frozen evidence, no communications, no writes to the input snapshot."""
import argparse
import json
import socket
import urllib.request
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from stage_compare import evaluate, ROOT

POLICIES={'fixed1':1,'fixed2':2,'fixed4':4,'fixed8':8,
          'expand1248':(1,2,4,8),'first4_rest':(4,64)}

def run(root,dataset):
    output={}
    for name,policy in POLICIES.items():
        result=evaluate(root,dataset,batch_size=policy)
        output[name]=result
    # The same mock, corpus and guarded adoption must give identical results.
    first=output['fixed1']['rows']
    for result in output.values():
        for a,b in zip(first,result['rows']):
            assert a['sample_id']==b['sample_id']
            assert a['staged_status']==b['staged_status']
            assert b['input_preserved'] and b['all_candidate_states_preserved']
            assert not b['staged_wrong_merge']
    return {'policies':output,'actual_model_accuracy':'unconfirmed','actual_api_cost':'unconfirmed',
            'real_api_calls':0,'benchmark':'single local CPU run; import time excluded; not API latency',
            'evidence':'same frozen provisional source-text labels; no independent human/body verification'}

def markdown(result):
    lines=['# 候補群比較（保存記事・模擬AI）','',
        '本文・独立した人の確認は未完了。同一9組・別事件9組は保存見出しに基づく暫定標本。要確認15組は正解率に含めない。',
        '模擬回答は候補の内容から実AIが推論した回答ではない。実AI精度・料金・通信待ち時間は未測定。',
        '','|方式|区分|組数|総入力バイト|候補群数|CPU合計ms|結果|','|---|---|---:|---:|---:|---:|---|']
    base=next(iter(result['policies'].values()))
    for name,run in [('現行全件',base),*result['policies'].items()]:
        old=name=='現行全件'
        for label,ja in [('same_event','同一（暫定）'),('different_event','別事件（暫定）'),('manual_review','要確認')]:
            rows=[r for r in run['rows'] if r['label']==label];p='legacy' if old else 'staged'
            statuses=dict(Counter(r[p+'_status'] for r in rows))
            lines.append(f"|{name}|{ja}|{len(rows)}|{sum(r[p+'_total_bytes'] for r in rows):,}|{sum(r['legacy_calls' if old else 'staged_batches'] for r in rows)}|{sum(r[p+'_ms'] for r in rows):.2f}|{statuses}|")
    lines+=['','全方式で同一標本の対象保持9/9・順位1位、別事件9組への誤統合0（模擬不正回答への防御テスト）。',
       '要確認の採用2組は正解とはみなさない。候補は削除せず、照合済みでも未解決ならexamined_unresolvedとして残る。',
       '','## 入力増加の理由と改善範囲','',
       '別事件の9組は対象との関係だけが別事件であり、全候補が別とは言えない。このため全34候補を最後まで調べても保留になる。',
       '小さい群ごとに指示・回答形式・同じ新記事の引用を再送するため、固定4件では9群に分かれて入力が増える。',
       'first4_restは最初の4候補で不成立なら残りを一括提示する。候補や根拠の削除・検証の緩和なしに重複指示を減らすが、後段は大きくなる。',
       '固定8件と比較し、実際の正解順位・保留率・予算停止を確認するまでは本番採用しない。',
       '全方式とも予算管理と既存の根拠検証を変更していない。今回の全候補比較では課金台帳への予約を無効にして入力量だけ測定した。予算不足の停止・再開は別テストで検証。',
       '','## 制約','',
       '同一標本は転載・同じ見出しが多く、難しい続報を代表しない。旧記事除去後も事件名・要約が残るため、未来情報を除いた時系列比較ではない。',
       'CPUは1回測定でSQLite準備時間を除く。入力バイト削減をトークン・料金削減率とは扱わない。']
    return '\n'.join(lines)+'\n'

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,default=ROOT);p.add_argument('--output-dir',type=Path,required=True)
    a=p.parse_args();root=a.snapshot.resolve();out=a.output_dir.resolve()
    if out.is_relative_to(root):p.error('output must be outside snapshot')
    dataset=json.loads((Path(__file__).parent/'evaluation.json').read_text())
    def deny(*args,**kwargs):raise AssertionError('network forbidden')
    with patch('urllib.request.urlopen',side_effect=deny),patch('socket.create_connection',side_effect=deny),patch.object(socket.socket,'connect',side_effect=deny):
        result=run(root,dataset)
    out.mkdir(parents=True,exist_ok=True)
    (out/'batch_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (out/'BATCH_RESULTS.md').write_text(markdown(result))
    print(markdown(result))

if __name__=='__main__':main()
