# INMERMUSIC BOT

[![CI](https://github.com/ryutek0821/DISCORD-MUSIC-BOT/actions/workflows/ci.yml/badge.svg)](https://github.com/ryutek0821/DISCORD-MUSIC-BOT/actions/workflows/ci.yml)

Discord でニコニコ動画・YouTube を再生できる Music Bot。

## 機能

- ニコニコ動画・YouTube の再生（URL または検索キーワード）
- キーワード検索時は上位5件（投稿者名・再生時間つき）から選択、プレイリスト/マイリストの一括追加
- キュー管理（追加・スキップ・表示・削除・クリア・シャッフル・リピート）
- 次曲プリフェッチ、再起動後のキュー復元、再生履歴・お気に入り・名前付きプレイリスト
- 再生中の曲情報を Embed で表示（タイトル・URL・再生位置プログレスバー・リクエスト者・サムネイル）＋操作ボタン
- 速度・ピッチ・音量の調整、シーク、エフェクトプリセット20種
- 効果音再生（`/na-` またはメッセージトリガー `んあー` / `んあーと`）
- ニコニコ Cookie の自動更新・手動更新
- アイドル時に自動 VC 切断

## スラッシュコマンド

| コマンド | 説明 |
|----------|------|
| `/play <URL/キーワード>` | 曲を再生（プレイリストURLは自動判定して一括追加） |
| `/playlist add <URL> [件数]` | プレイリストを重複除外して一括追加 |
| `/playlist save/load/list/delete` | 現在のキューをサーバー共有の名前付きリストとして保存・管理 |
| `/skip` | 現在の曲をスキップ |
| `/queue` | キューを10件ずつページ表示、各曲の開始ETAも表示 |
| `/previous` / `/replay` | 前の曲へ戻る／現在曲を先頭から再生 |
| `/history` / `/historyplay <番号>` | 最近の再生履歴を表示／キューへ再追加 |
| `/favorite` / `/favorites` | 再生中の曲をお気に入り保存／一覧表示 |
| `/playfavorite <番号>` / `/unfavorite <番号>` | お気に入りを再生／削除 |
| `/loop <off/song/queue>` | リピート再生（オフ／1曲／キュー全体） |
| `/shuffle` | キューをシャッフル |
| `/speed <0.5-2.0>` | 再生速度を変更（ピッチ維持） |
| `/pitch <-12〜+12>` | ピッチを半音単位で変更 |
| `/volume <0-200>` | 音量を変更（%） |
| `/seek <時間>` | 再生位置へジャンプ（秒 または `mm:ss`） |
| `/preset <名前>` | エフェクトプリセット適用（下記20種） |
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
| `/refresh` | ニコニコのCookieを手動で更新（要 サーバー管理権限） |
| `/settings [音量] [切断秒数]` | サーバー既定値を表示・変更（要 サーバー管理権限） |

### 実行制限

- DM では使用できません（サーバー内専用）
- Bot が VC に接続中は、**同じ VC に参加しているユーザーのみ**操作できます。ただし閲覧系（`/help` `/queue` `/nowplaying` `/history` `/favorites` `/playlist list` `/playlist delete`）は VC 外からでも実行可能です
- `/refresh` と `/settings` は `サーバー管理` 権限が必要です

再生中の曲の Embed には操作ボタン（⏯️ 一時停止/再開・⏭️ スキップ・⏹️ 停止・🔁 リピート・🔀 シャッフル）とエフェクト選択ドロップダウンが付きます。

## エフェクトプリセット

`/preset` と now-playing のドロップダウンから選べます（`off` で解除）。

| キー | 表示名 | キー | 表示名 |
|------|--------|------|--------|
| `nightcore` | ナイトコア | `chipmunk` | チップマンク |
| `vaporwave` | ベイパーウェイブ | `deep` | 重低音ボイス |
| `bassboost` | 低音ブースト | `chorus` | コーラス |
| `trebleboost` | 高音ブースト | `phaser` | フェイザー |
| `8d` | 8Dオーディオ | `flanger` | フランジャー |
| `lofi` | Lo-Fi | `vibrato` | ビブラート |
| `echo` | エコー | `telephone` | 電話越し |
| `reverb` | リバーブ | `crystalizer` | クリスタル |
| `tremolo` | トレモロ | `wide` | ワイドステレオ |
| `karaoke` | ボーカルカット | `underwater` | 水中 |

プリセットは `config.py` の `EFFECT_FILTERS` / `EFFECT_PRESETS` / `EFFECT_LABELS` / `EFFECT_EMOJI` が唯一の定義元です。4つのテーブルに追加すれば `/preset` の選択肢とドロップダウンへ自動反映されます（UI 側の変更は不要）。

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
pip install -r requirements-dev.txt

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
STATE_DIR=~/.local/share/inmermusic  # DB保存先（既定値。リポジトリ外である必要あり）
DOWNLOAD_DIR=/var/tmp/inmermusic     # 音声一時ファイルの保存先（既定値）
IDLE_TIMEOUT=180         # アイドル切断時間（秒）
DOWNLOAD_TIMEOUT=120     # 1曲のDLタイムアウト（秒）
MAX_TRACK_DURATION=1800  # 1曲の最大長（秒）
MAX_QUEUE_SIZE=100       # Guildごとのキュー上限
MAX_PLAYLIST_SIZE=50     # 1回に追加するプレイリスト上限
MAX_PLAYLISTS_PER_GUILD=25 # Guildごとの保存プレイリスト上限
PREFETCH_MAX_BYTES=268435456 # Guildごとの次曲先読み上限（bytes）
LOG_FILE=bot.log         # 指定時はローテーションログも出力
YT_PROXY=http://your-proxy-host:8888  # YouTube用プロキシ（住宅IPが必要な場合）
YT_PROXIES=http://primary:8888,http://secondary:8888 # 複数プロキシのフェイルオーバー
```

> ログは標準出力（systemd 運用時は journald が収集）に出ます。`LOG_FILE` を設定すると 5MB×3 世代のローテーションファイルにも出力します。

> **`STATE_DIR` はリポジトリ外を指す必要があります。** デプロイは `rsync --delete` でリポジトリを丸ごと同期するため、リポジトリ内に置くと更新のたびにDBが消えます。リポジトリ内のパスを指定した場合は警告を出して既定値（`~/.local/share/inmermusic`）へ強制的に戻します。ディレクトリとDBは `0700` / `0600` で作成されます。

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

`sounds/na-.mp3` に効果音ファイルを配置してください。`/sound <名前>` は `sounds/` 配下の音源を名前で再生します（パストラバーサルは `config.resolve_sound()` で防止）。

## 開発

Discord への接続なしでテストできます。

```bash
ruff check .            # Lint（E9 構文エラー + F pyflakes のみを対象）
pytest tests/ -q        # テスト（CI と同じ呼び出し）
python tests/test_features.py   # 依存なしの単体実行も可能
```

CI（`ci.yml`）は Python 3.11 / 3.12 の両方で ruff → `compileall` → pytest を実行します。

## アーキテクチャ

```
inmermusic/
├── bot.py       # Bot 起動エントリポイント
├── cog.py       # スラッシュコマンド定義（discord.ext.commands.Cog）
├── state.py     # GuildState クラス（サーバーごとのキュー・VC・再生状態）
├── playback.py  # 再生制御ロジック（キュー送り・ループ・スキップ）
├── audio.py     # FFmpeg フィルタ構築・音源生成・ダウンロード・検索/プレイリスト展開
├── ui.py        # Embed・操作ボタン UI
├── cookies.py   # ニコニコ Cookie 取得・更新（APIログイン + Seleniumフォールバック）
├── persistence.py # キュー・履歴・お気に入り・Guild設定のSQLite永続化
├── nico_cli.py  # Guild別ニコニコセッションのローカル管理CLI
├── config.py    # 環境変数読み込み・ログ設定・エフェクト/プリセット定義テーブル
└── util.py      # 共通ユーティリティ
main.py          # エントリポイント（inmermusic.bot を呼び出す）
```

- ニコニコ動画の音声は yt-dlp でローカルにダウンロードしてから再生（403 エラー回避）
- YouTube もプロキシ制約のため一時ファイルへダウンロードして再生
- 再生終了後に一時ファイルを自動削除
- 再生中に次の1曲を先読み（Guildあたり1曲まで）。キューの並び替え・シャッフル時は先頭以外のキャッシュを破棄
- Cookie は API ログインで取得、失敗時に Selenium フォールバック

### 永続化と再起動時の挙動

キュー・履歴・お気に入り・名前付きプレイリスト・Guild設定は `STATE_DIR/music.db`（SQLite / WAL）に保存されます。

- キューはコマンド操作のたびに保存されるため、クラッシュしても直前の状態が残ります。再生中だった曲はキュー先頭として保存されます
- 再起動後、そのGuildで最初にコマンドが実行された時点でキューと設定（既定音量・アイドル切断秒数・リピートモード）を復元します。復元は特定のコマンド専用ではなく、`/play` や `/queue` など状態を扱うコマンド全般が契機になります（`/help` `/history` `/favorites` `/refresh` `/settings` `/playlist list` `/playlist delete` は除く）
- Bot は自動では再生を再開しません。`/play` すると復元されたキューの先頭から再生され、新規追加分は Embed のフッターに件数が表示されます
- `/stop` `/leave` とアイドル切断時は保存済みキューも消去します

## Guild別ニコニコセッション

セッショントークンをDiscordへ送らず、Botホスト上のCLIで登録します。

```bash
# 非表示プロンプトから登録
python -m inmermusic.nico_cli set 123456789012345678

# ファイルから登録、状態確認、削除
python -m inmermusic.nico_cli set 123456789012345678 --session-file /secure/session.txt
python -m inmermusic.nico_cli status 123456789012345678
python -m inmermusic.nico_cli list
python -m inmermusic.nico_cli delete 123456789012345678
```

`guilds.db` と生成Cookieは `STATE_DIR` 配下に0600で保存されます。

## GitOps

自動化された GitOps ワークフロー：

1. **PR → CI**（`ci.yml`）: Python 3.11/3.12でruff + pytestを実行
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
| `SSH_KNOWN_HOSTS` | デプロイ先の固定済みknown_hosts行 |
| `PAT_FOR_AUTOMERGE` | auto-merge ワークフロー用 Personal Access Token |
