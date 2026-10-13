# 進展検知の隔離シミュレーション

このディレクトリはテスト専用です。架空続報は `https://simulation.invalid/` を使い、実在ニュースとして収集・公開しません。
`fixtures/real_event_snapshot.json` は main の ff25fb80910c3621a096b8c2e1e9cea3263376e2 から採った3イベントの固定スナップショットです。ニュースデータの通常更新に依存せず回帰テストを実行できます。

実行例（出力はリポジトリ外の絶対パス）：

```sh
python -m unittest discover -s tests -v
python simulation/run_progress_simulation.py /tmp/news-now-progress-preview
```

シミュレーションはFakeAIを使い、実APIも検索も呼びません。AIの意味判定精度を保証するものではありません。
`events.json` から `public_events.json` を作る経路は一時ディレクトリ内で実行し、実データ5ファイルのハッシュ不変を確認します。

表示のオフライン検査用 `verify_display.cjs` は実際の index.html の JavaScript を DOM double で実行します。比較結果（baseline/proposed）を揃えて `node simulation/verify_display.cjs OUTPUT` で確認できます。ブラウザのレイアウト・スマホ幅・スクリーンショットを保証するものではありません。
