# INMERMUSIC BOT

[![CI](https://github.com/ryutek0821/INMERMUSIC_BOT/actions/workflows/ci.yml/badge.svg)](https://github.com/ryutek0821/INMERMUSIC_BOT/actions/workflows/ci.yml)

Discord でニコニコ動画・YouTube を再生できる Music Bot。

## 機能

- ニコニコ動画・YouTube の再生（URL または検索キーワード）
- キュー管理（追加・スキップ・表示・削除・クリア・シャッフル・リピート）
- 再生中の曲情報を Embed で表示（タイトル・URL・再生位置プログレスバー・リクエスト者・サムネイル）＋操作ボタン
- 速度・ピッチ・音量の調整、シーク、エフェクトプリセット（ナイトコア／ベイパーウェイブ／低音ブースト／8D／Lo-Fi）
- 効果音再生（`/na-` またはメッセージトリガー `んあー` / `んあーと`）
- ニコニコ Cookie の自動更新・手動更新
- アイドル時に自動 VC 切断

## スラッシュコマンド

| コマンド | 説明 |
|----------|------|
| `/play <URL/キーワード>` | 曲を再生（ニコニコ動画URL、YouTubeURL、または検索キーワード） |
| `/skip` | 現在の曲をスキップ |
| `/queue` | キューを表示（最大10件） |
| `/loop <off/song/queue>` | リピート再生（オフ／1曲／キュー全体） |
| `/shuffle` | キューをシャッフル |
| `/speed <0.5-2.0>` | 再生速度を変更（ピッチ維持） |
| `/pitch <-12〜+12>` | ピッチを半音単位で変更 |
| `/volume <0-200>` | 音量を変更（%） |
| `/seek <時間>` | 再生位置へジャンプ（秒 または `mm:ss`） |
| `/preset <名前>` | エフェクトプリセット適用（ナイトコア／ベイパーウェイブ／低音ブースト／8D／Lo-Fi） |
| `/effect <名前> <値>` | 個別エフェクト設定 |
| `/move <from> <to>` | キュー内の曲を並び替え |
| `/remove <番号>` | 指定したキューの曲を削除 |
| `/clear` | キューをクリア（再生中の曲は継続） |
| `/stop` | 再生を停止してキューをクリア、VCから切断 |
| `/pause` | 現在の曲を一時停止 |
| `/resume` | 一時停止した曲を再開 |
| `/nowplaying` | 再生中の曲情報を表示 |
| `/join` | あなたのVCにBotを接続 |
| `/leave` | BotをVCから切断 |
| `/help` | コマンド一覧を表示 |
| `/na-` | 効果音を再生（同一楽曲中に1回のみ） |
| `/sound <名前>` | サウンドボードの音源を再生 |
| `/refresh` | ニコニコのCookieを手動で更新 |

再生中の曲の Embed には操作ボタン（⏯️ 一時停止/再開・⏭️ スキップ・⏹️ 停止・🔁 リピート・🔀 シャッフル）が付きます。

## メッセージトリガー

VC 接続中に以下のメッセージを送信すると効果音が再生されます：
- `んあー`
- `んあーと`

## 必要環境

- Raspberry Pi 4 (aarch64) または Linux/macOS
- Python 3.11+
- FFmpeg
- Chromium（Selenium フォールバック用）

## セットアップ

```bash
# 仮想環境作成
python3 -m venv venv
source venv/bin/activate

# 依存関係インストール
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env
# .env ファイルを編集して各値を設定
```

## .env 設定

`.env.example` をコピーして編集してください。

```
# 必須
DISCORD_TOKEN=your_discord_bot_token
COOKIE_FILE=cookies.txt
NICO_EMAIL=your_niconico_email
NICO_PASSWORD=your_niconico_password

# オプション
CHROMEDRIVER_PATH=/usr/bin/chromedriver
COOKIE_TTL=3600          # Cookie有効期限（秒）
IDLE_TIMEOUT=180         # アイドル切断時間（秒）
DOWNLOAD_TIMEOUT=120     # 1曲のDLタイムアウト（秒）
LOG_FILE=bot.log         # 指定時はローテーションログも出力
YT_PROXY=http://your-proxy-host:8888  # YouTube用プロキシ（住宅IPが必要な場合）
```

> ログは標準出力（systemd 運用時は journald が収集）に出ます。`LOG_FILE` を設定すると 5MB×3 世代のローテーションファイルにも出力します。

## 起動

```bash
python main.py
```

### systemd サービスとして起動

```bash
sudo cp niconico-bot.service /etc/systemd/system/
sudo systemctl enable niconico-bot
sudo systemctl start niconico-bot
```

## 効果音

`sounds/na-.mp3` に効果音ファイルを配置してください。

## アーキテクチャ

```
inmermusic/
├── bot.py       # Bot 起動エントリポイント
├── cog.py       # スラッシュコマンド定義（discord.ext.commands.Cog）
├── state.py     # GuildState クラス（サーバーごとのキュー・VC・再生状態）
├── playback.py  # 再生制御ロジック（キュー送り・ループ・スキップ）
├── audio.py     # FFmpeg オプション生成・エフェクト・プリセット定義
├── ui.py        # Embed・操作ボタン UI
├── cookies.py   # ニコニコ Cookie 取得・更新（APIログイン + Seleniumフォールバック）
├── config.py    # 環境変数読み込み・設定
└── util.py      # 共通ユーティリティ
main.py          # エントリポイント（inmermusic.bot を呼び出す）
```

- ニコニコ動画の音声は yt-dlp でローカルにダウンロードしてから再生（403 エラー回避）
- YouTube はストリーム再生
- 再生終了後に一時ファイルを自動削除
- Cookie は API ログインで取得、失敗時に Selenium フォールバック

## GitOps

自動化された GitOps ワークフロー：

1. **PR → CI**（`ci.yml`）: ruff + `tests/test_features.py` を実行
2. **CI green → Auto merge**（`auto-merge.yml`）: CI 成功時のみ squash マージ
3. **Auto merge → Deploy**: マージ後に `deploy-on-push.yml` を dispatch し RYU-RASPBERRYPI へ反映
   （`GITHUB_TOKEN` のマージは `push` を発火しないため明示 dispatch している）
4. **master へ直接 push**: `deploy-on-push.yml`（deploy）と `version-tag.yml`（タグ付け）が発火

> CI が赤の PR はマージされない。`niconico-bot.service` はリポジトリ管理下にあり、上記「systemd サービスとして起動」の手順で `/etc/systemd/system/` に配置する。

### 必要な GitHub Secrets

| Secret名 | 内容 |
|----------|------|
| `TAILSCALE_AUTHKEY` | Tailscale のオースキー（Ephemeral推奨） |
| `SSH_HOST` | デプロイ先サーバーのIP（Tailscale IP） |
| `SSH_KEY` | デプロイ用SSHプライベートキー |
| `PAT_FOR_AUTOMERGE` | auto-merge ワークフロー用 Personal Access Token |
