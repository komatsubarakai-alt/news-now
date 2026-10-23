# ニュースの現在地：公開環境の比較と推奨構成

確認日：2026年10月8日。月額300円以下の有料プランを想定した設計資料。
**移行・契約・ドメイン購入・課金開始は行わない。現在のGitHub Pages、収集Actions、追跡エンジンは変更しない。**

## 提案

第一候補は **Cloudflare Workersの静的配信＋Workers API＋D1、Firebase Authentication／FCM、Stripe Checkout／Billing**。
無料公開は無料枠から始め、有料会員機能を提供するときはWorkers Paid（月額最低5 USD）を予算化する。Paid加入は商用利用の必須条件という意味ではなく、無料枠の日次停止とCPU制約を避けるための運用判断。
第二候補はFirebaseで公開・認証・通知をまとめる構成。サービスを分けたくない場合はこちらが扱いやすい。

現在は静的HTMLとJSONを配信し、Pythonの収集・分類・追跡はGitHub Actionsで動く。公開サーバーへPython処理を移す必要はない。
閲覧数に応じてAIを呼ばず、一度作ったニュースの現在地を共有する構成が低価格プランに合う。

## 費用の見方

以下はサービス利用料。開発作業の人件費、AI API、ドメイン、メール配信、適用される税、為替・カードの海外決済費、追加バックアップ等は別。無料クレジットや期間限定の割引は見積もりに含めない。
USD料金の比較用に **1 USD＝150円と仮定**した金額も示す。現在の為替レートではない。実際の請求額は契約時に再確認する。

| 候補 | サービス初期費用 | 無料公開・試作 | 有料会員を始める際の月額目安 | メリット | デメリット |
| --- | --- | --- | --- | --- | --- |
| A：Cloudflare Workers静的配信＋D1＋Firebase認証・FCM | 0円。独自ドメイン取得は別 | 0 USD、無料枠内 | Workers最低5 USD（仮定750円）＋超過利用。認証・FCMは選ぶ方式と枠による | 静的配信は無料・無制限。少ない固定費、API・SQL保存を追加可能 | CloudflareとFirebaseを連携する実装が必要。D1の容量・同時処理制約を考慮 |
| B：Firebase Hosting＋Auth＋Firestore＋Functions＋FCM | 0円。独自ドメイン取得は別 | Sparkは0 USD、枠超過で停止するサービスあり | Blazeは固定月額なし、使った分。Functionsの利用にはBlazeが必要 | 認証・通知・DBをまとめやすい。小規模なら無料枠中心 | 転送・DB読み書き・関数等を合算。無料枠を超えると増額し、予算通知だけでは上限にならない |
| C：Vercel Pro＋Supabase | 0円。独自ドメイン取得は別（Vercelのドメイン特典は対象条件確認） | Vercel Hobbyは個人・非商用限定。将来の商用基盤として無料Hobbyを前提にしない | Pro 20 USD＋Supabase Freeなら20 USD（仮定3,000円）。Supabase Pro 1プロジェクトなら合計45 USD（仮定6,750円）から | GitHub連携・プレビューが便利。PostgreSQLと会員機能を整えやすい | 本番向けDBを含めると固定費が高い。Supabase Freeは500 MB、非活動1週間で停止、バックアップ条件も異なる |

Aの静的配信はCloudflare Pagesでも可能だが、新規構成では静的配信とAPIを同じWorkers基盤にまとめる案を優先する。Cloudflareの一般的なWebサイト向けPro契約とWorkers Paidは別の料金体系で、この案にWebサイト向けProは含めない。
Cの45 USDは必要最低額の断定ではなく、1開発者・DB本番運用を重視してSupabase Proを選んだ比較構成。

## 機能と連携

| 項目 | A：Cloudflare中心 | B：Firebase中心 | C：Vercel＋Supabase |
| --- | --- | --- | --- |
| 独自ドメイン・HTTPS | 対応。Workers Custom DomainsはCloudflare上の有効なゾーンが必要。登録会社は別でもよい | Hostingで対応 | 対応。サイトのドメインとSupabase APIの有料ドメイン機能は別 |
| 決済 | WorkersでCheckout作成・署名付きWebhook受信、Stripe連携を実装 | FunctionsでStripe連携を実装 | Vercel Functions等でStripe連携を実装 |
| 会員認証 | Firebase Authを利用。Cloudflare Accessの管理者向けログインを有料会員認証と混同しない | Firebase Auth | Supabase Auth |
| 保存データ | D1にウォッチ対象、契約状態、配信履歴等 | Firestoreに同等の情報 | PostgreSQLに同等の情報 |
| 通知 | FCMのWeb Push＋Workers側の配信処理 | FCM＋Functions | FCM等を別途利用＋配信処理 |
| GitHub Actions | Wranglerで公開データ・サイトを配信可能 | 公式Hosting Action／CLI | 公式CLIで配信可能 |
| 北海道・全国展開 | 地域別の静的配信で閲覧増を吸収。検索API・D1は計測して拡張 | CDN配信、地域別検索。転送とDB操作の増加を計測 | CDN＋PostgreSQLで拡張。CDN容量階層とDB性能・超過料金を計測 |

決済・認証・通知は、ホスティングを選んだだけで自動的に完成するものではない。会員IDとStripe顧客IDの対応、契約更新・失敗・解約の反映、通知許可と停止、実機検証が必要。
Firebase Authは電話／SMSを初期案から外し、Googleログインまたはメール・パスワードを候補とする。Identity Platformを有効にする場合の標準方式は月5万MAUまで無償、その後従量。通常のAuthとIdentity Platformの条件、日次制限を導入時に確認する。
FCM自体は無償だが、配信するAPI・DB・再試行処理は別費用。メール通知は送信事業者の費用が別なので、初期固定費に無料として含めない。

## アクセス増加時の料金

| 項目 | 無料・含まれる枠 | 超過・制約 |
| --- | --- | --- |
| A：Workers静的アセット | 静的配信のリクエストは無料・無制限 | アセットより先にWorkerを動かす設定やWorkers Caching経由では課金扱いが変わる。認証APIは動的 |
| A：動的API | Freeは10万リクエスト／日。Paidは月1,000万リクエスト・3,000万CPU ms込み | Paid超過：100万リクエストごと0.30 USD、100万CPU msごと0.02 USD。5 USDは請求の上限ではない |
| A：D1（Paid） | 月250億行読み取り・5,000万行書き込み、保存5 GB込み | 超過：100万読取行0.001 USD、100万書込行1 USD、保存0.75 USD／GB月。インデックス更新も書込み |
| B：Hosting | 保存10 GB、転送10 GB／月まで無償（詳細資料） | 保存超過0.026 USD／GB、転送超過0.15 USD／GB。Sparkは転送超過で猶予後に停止 |
| B：Firestore Standard | 保存1 GiB、読取5万／日、書込2万／日等の無料枠 | 地域と利用量に応じた従量。画面を開くたびに全ニュースをDBから読むと増えやすい |
| C：Vercel Pro | 月20 USDの利用クレジット。Flat Rate CDNを有効にする場合、初段は100万CDNリクエスト・1 TB込み | Flat Rate CDNの次段は追加20 USD／月で1,000万リクエスト・50 TB。継続的な容量超過は翌期に上段へ。関数等は別の課金項目 |
| C：Supabase Pro | 1プロジェクト、DB8 GB、10万MAU、egress250 GB、cached egress250 GB等 | DB0.125 USD／GB、認証0.00325 USD／MAU、egress0.09 USD／GB、cached0.03 USD／GB等。計算資源の増強は別 |

Firebase料金一覧にはHosting転送「360 MB／日」、更新された詳細資料には「10 GB／月」と記載差がある。本書は2026年10月7日更新のHosting詳細資料を計算に使用する。実契約・管理画面で適用枠を確認し、日次と月次を足し合わせない。Firebase App Hostingは別製品で、本書のHosting見積もりに混ぜない。

**計算例（予測ではない）**：1閲覧で合計0.5 MBを転送すると仮定した場合。

| 月間閲覧 | 転送量の仮定 | Aの静的配信料 | BのHosting転送料（10 GB控除） |
| --- | --- | --- | --- |
| 1万 | 5 GB | 0 USD | 0 USD |
| 10万 | 50 GB | 0 USD | 6 USD |
| 100万 | 500 GB | 0 USD | 73.50 USD |

MB/GBはこの例では十進。DB・API・SDKの別配信、キャッシュ、画像等で実際の転送は変わる。PVはAPI呼出回数ではない。
Aの動的APIが月1,500万回、平均CPU 7 msならWorkers分は5＋1.50＋1.50＝8 USD。DB・認証・通知・AI等を加えた総額ではない。

## 独自ドメインと初期費用

サービスのセットアップ料金は各候補0円。独自ドメインを購入するなら初年度登録料が別途発生し、翌年以降も更新料がかかる。
ドメイン名・拡張子・既存所有の有無が未決定なので、取得価格を確定額として示すことはできない。**年2,000〜3,000円を仮の予算枠**とし、候補を決めた段階で初年度と更新年の両方を見積もる。この範囲は公式価格の引用や保証ではない。
Cloudflare Registrarは登録・更新とも原価方式。安い初年度だけを基準に選ばない。HTTPS証明書の別購入はこの構成では想定しない。
Stripe CheckoutのURLまで独自ドメイン化するオプション（月10 USD）は採用せず、サイトの独自ドメインだけを用意する案。

## 月額300円以下で成立するか

日本の標準カード決済3.6%＋Stripe Billing従量0.7%を仮定。初期・固定月額は標準従量構成で0円。

| 利用料金案 | 概算決済・Billing手数料 | 手数料控除後（他費用控除前） |
| --- | --- | --- |
| 月280円 | 12.04円 | 267.96円／人月 |
| 月300円 | 12.90円 | 287.10円／人月 |
| 280円×100人 | 1,204円 | 26,796円／月 |

割合による概算で、実際の丸め、税の適用、返金・不審請求、追加サービス、通貨換算は含めない。これは利益ではない。コンビニ決済は最低手数料120円なので低額月払いの初期案に向かない。初期は円建てカード定期課金を候補にする。

必要会員数＝切り上げ（月の運営費合計 ÷ 267.96円）。280円プランで、月運営費が仮に2,000円なら8人、5,000円なら19人、10,000円なら38人が目安。人件費と税の控除前。運営費にはホスト、ドメイン年額の1/12、**既存のAI API全処理**、通知、バックアップ等を含める。
実際のAI請求額は未確認なので、現段階では「月額300円以下で利益が出る」とは断定しない。全国展開では閲覧数より収集対象・AI判定量の増加が先に課題になる可能性がある。

## 推奨構成の役割

1. 公開画面：Cloudflare Workersの静的配信。無料閲覧・根拠記事・5カテゴリーを公開。
2. 認証：Firebase Auth。閲覧はログイン不要、ウォッチリストを使う人だけログイン。
3. 有料機能API・DB：Workers＋D1。本人のウォッチ対象、契約状態、通知履歴を保存。会員データは公開GitHubや配信用JSONに入れない。
4. 決済：Stripe Checkout／Billing＋Customer Portal。WorkerでWebhook署名と重複を確認し、サーバー側の契約状態で有料権限を判定。戻り先画面だけで支払済みにしない。
5. 通知：FCMを候補にする。重要な進展・予定接近をまとめ、配信停止と重複防止を用意。iPhoneはiOS/iPadOS 16.4以降のホーム画面追加Webアプリ等の条件があり、実機で確認する。到達時刻を保証する防災速報として販売しない。
6. 収集・追跡：当面GitHub Actionsに残す。成功して保存された公開データを、後段の配信処理へ渡す。課金・通知障害で収集を停止させない。

GitHub連携では、現在のActionsがGITHUB_TOKENでJSONをコミットするため、通常のpushを契機にした別Actionsが再実行されるとは限らない。移行を実施するなら、収集完了後の独立したworkflow_run等で、成功確認・最新保存SHAの固定・データ検証を行う。過去のhead_shaにはその実行が後から保存したJSONがない場合がある。新しい配信処理を今のテスト中に有効化しない。
公開用ディレクトリに必要ファイルだけを出力し、リポジトリ全体や会員情報、秘密鍵を配信しない。Firebaseトークン検証とFCM送信のWorkers互換性は実装前に小さく確認する。

## 北海道から全国へ広げる順序

- 同じ独自ドメインの地域ページとして札幌、北海道、全国を追加する案。地域ごとにホスティング契約を増やさない。カテゴリーは5種類のまま、地域とカテゴリーを別の絞り込み項目にする。
- 地域／更新日ごとに公開データを小分けにし、ページングする。全地域・全履歴のJSONを毎回読み込まない。ニュースは一度生成して共有し、アクセスごとにAIを再実行しない。
- 全国に関係する出来事は同じIDを複数地域で参照し、地域別に重複生成・重複通知しない設計を検討。
- D1はPaidでも1 DB最大10 GB、1 DBの処理は直列。最初から47 DBを作らず、容量・検索時間・同時負荷を測り、必要時に分割や別DBを検討。件数だけから収容可能人数を断定しない。
- 有料会員を地域別DBに分散する必要は当初ない。契約は共通、ウォッチ対象と地域設定を分ける。
- 収集範囲を増やす前にAIトークン数、1回の処理時間、RSS取得条件と対象件数を測る。札幌向けの現行処理が全国規模でもそのまま動くとは扱わない。
- 予算アラートに加え、APIの頻度制限、CPU制限、通知のまとめ配信、AIの呼出・トークン上限を設計。アラートを請求の強制上限と混同しない。

## テスト終了時に判断できるようにすること

現時点では比較資料だけを準備する。10月15日の検証結果と実際のAI費を合わせ、公開対象地域、独自ドメイン、無料公開先、有料機能の範囲を判断する。
その後、隔離した公開環境でデータ配信・スマホ表示を確認し、認証、Stripeテストモード、通知の実機テストを順に進める。現在のテストURLを止める時期や本番課金への切替は別途確認する。

## 公式資料

- Cloudflare Workers料金：https://developers.cloudflare.com/workers/platform/pricing/
- 静的配信の料金・制約：https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/
- D1料金：https://developers.cloudflare.com/d1/platform/pricing/
- D1制約：https://developers.cloudflare.com/d1/platform/limits/
- Workers独自ドメイン：https://developers.cloudflare.com/workers/configuration/routing/custom-domains/
- Cloudflareドメイン登録：https://developers.cloudflare.com/registrar/
- Cloudflare GitHub Actions：https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/
- Firebase料金：https://firebase.google.com/pricing
- Firebase Hosting詳細料金：https://firebase.google.com/docs/hosting/usage-quotas-pricing
- Firebase GitHub連携：https://firebase.google.com/docs/hosting/github-integration
- Firebase予算管理：https://firebase.google.com/docs/projects/billing/avoid-surprise-bills
- Vercel料金：https://vercel.com/pricing
- Vercel Hobby条件：https://vercel.com/docs/plans/hobby
- Vercel Flat Rate CDN：https://vercel.com/docs/pricing/flat-rate-cdn
- Vercel GitHub Actions：https://vercel.com/kb/guide/how-can-i-use-github-actions-with-vercel
- Supabase料金：https://supabase.com/pricing
- Stripe日本料金：https://stripe.com/jp/pricing
- Stripe Billing料金：https://stripe.com/jp/billing/pricing
- iPhone Web Push条件：https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/
- GitHub Actionsの起動条件：https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
- GitHub Pagesの商用条件：https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits

GitHub Pagesはオンライン事業・商取引・商用SaaSを主目的とした無料ホスティングには制限がある。有料会員サービスを開始する前に公開基盤を分ける判断が必要。外部の決済ページを使えば条件を自動で満たすとは解釈しない。
