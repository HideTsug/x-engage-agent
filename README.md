# x-engage-agent

Claude Code が駆動する X（旧Twitter）エンゲージメント支援エージェント。
ニッチ（同梱テンプレートは**税理士法人**向け＝個人事業主・中小企業経営者・起業家などの顧問先候補）の関連投稿を検索・スコアリングし、**いいね/フォローすべき候補リスト**を生成して **Chatwork に配信**します。配信メッセージには X アプリで直接開けるリンクが並ぶので、タップするだけで手動エンゲージできます。

> **なぜ「手動」なのか**: X API のセルフサーブ全ティアで `likePost` / `followUser` が廃止されたため、いいね・フォローの完全自動化はできません。本ツールは「誰に・なぜエンゲージすべきか」の判断（検索→スコアリング→除外）を自動化し、実行アクションだけ人間に渡します。

## アーキテクチャ

```
improver/engage.sh                      … オーケストレーター
  ├─ xmcp/server.py                     … X API v2 を叩く MCP サーバ（OAuth1）
  ├─ scripts/engagement/fetch_following.py … フォロー/フォロワーのキャッシュ更新・アンフォロー候補生成
  ├─ prompts/engager.md                 … Claude Code に渡すエージェント指示（検索→採点→除外→記録→通知）
  └─ scripts/notify.py                  … Chatwork へ候補リストを配信
```

候補・キャッシュは `improver/data/engagement/` に保存されます（`.gitignore` 済み・コミットされません）。

## 必要なもの

- [Claude Code CLI](https://docs.claude.com/claude-code)（`claude` コマンド）
- Python 3.11+
- X API アプリ（OAuth1 Consumer Key/Secret、Bearer Token）— [developer.x.com](https://developer.x.com)
- Chatwork アカウントと [API トークン](https://www.chatwork.com/service/packages/chatwork/subpackages/integrations/api.php)

## セットアップ

トークン類はすべて **fork した側の環境変数 / `.env`** で設定します。リポジトリには一切コミットされません。

```bash
# 1. fork してクローン
git clone https://github.com/<your-account>/x-engage-agent.git
cd x-engage-agent

# 2. X API 認証情報（xmcp）
cp xmcp/env.example xmcp/.env
# xmcp/.env を編集: X_OAUTH_CONSUMER_KEY / X_OAUTH_CONSUMER_SECRET / X_BEARER_TOKEN ほか

# 3. Chatwork と対象アカウント（リポジトリルート）
cp .env.example .env
# .env を編集: CHATWORK_API_TOKEN / CHATWORK_ROOM_ID / X_USERNAME

# 4. xmcp の仮想環境（engage.sh が source します）
python3 -m venv xmcp/.venv
xmcp/.venv/bin/pip install -r xmcp/requirements.txt -r requirements.txt
```

`xmcp/.env` の各項目の意味は [`xmcp/README.md`](xmcp/README.md) を参照してください。OAuth1 アクセストークンの取得は xmcp サーバの OAuth フローで行えます。

## 実行

```bash
# プレビュー（API を叩かず動作確認）
bash improver/engage.sh --dry-run

# 本実行（検索→採点→Chatwork 配信）
bash improver/engage.sh
```

cron などで毎日回す場合は `improver/engage.sh` をそのままスケジュールしてください。1 セッション 25 分以内、X API 検索は最大 12 回に制限されています（`prompts/engager.md` 参照）。

## カスタマイズ

- **ニッチの変更**: `prompts/engager.md` の検索クエリ例とスコアリング基準を自分の領域に書き換えます。
- **除外リスト**: `improver/data/engagement/internal_blocklist.json`（身内）、`churned_blocklist.json`（churned）を置くと候補から除外されます。
- **通知先の差し替え**: Chatwork 以外に流したい場合は `improver/scripts/notify.py` を差し替えてください（`notify.py "<title>" "<body>"` のインターフェースを保てば engage.sh / engager.md 側は変更不要）。

## セキュリティ

- `.env` と `xmcp/.env` はコミットされません（`.gitignore` 済み）。
- `improver/data/engagement/` のフォロー状態・候補履歴もコミットされません。
- トークンを誤って入れた場合は X / Chatwork 側で必ずローテートしてください。

## ライセンス

MIT
