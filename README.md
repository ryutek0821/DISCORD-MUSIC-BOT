# INMERMUSIC_BOT

Discordでニコニコ動画・YouTubeを再生できるMusic Bot

## 機能

- ニコニコ動画・YouTubeの再生（URL または検索キーワード）
- キュー管理（追加・スキップ・表示・削除・クリア・シャッフル・リピート）
- 再生中の曲情報をEmbedで表示（タイトル・URL・再生位置プログレスバー・リクエスト者・サムネイル）＋操作ボタン
- 速度・ピッチ・音量の調整、シーク、エフェクトプリセット（ナイトコア／ベイパーウェイブ／低音ブースト／8D／Lo-Fi）
- 効果音再生（`/na-` またはメッセージトリガー `んあー` / `んあーと`）
- ニコニコCookieの自動更新・手動更新
- アイドル時に自動VC切断

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
| `/refresh` | ニコニコのCookieを手動で更新 |

再生中の曲のEmbedには操作ボタン（⏯️ 一時停止/再開・⏭️ スキップ・⏹️ 停止・🔁 リピート・🔀 シャッフル）が付きます。

## メッセージトリガー

VC接続中に以下のメッセージを送信すると効果音が再生されます：
- `んあー`
- `んあーと`

## 必要環境

- Raspberry Pi 4 (aarch64) または Linux/macOS
- Python 3.11+
- FFmpeg
- Chromium（Seleniumフォールバック用）

## セットアップ

```bash
# 仮想環境作成
python3 -m venv venv
source venv/bin/activate

# 依存関係インストール
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env
# .envファイルを編集して各値を設定
```

## .env設定

```
DISCORD_TOKEN=あなたのDiscordBotトークン
COOKIE_FILE=cookies.txt
NICO_EMAIL=ニコニコメールアドレス
NICO_PASSWORD=ニコニコパスワード
CHROMEDRIVER_PATH=/usr/bin/chromedriver  # オプション
COOKIE_TTL=3600                           # オプション、Cookie有効期限（秒）
IDLE_TIMEOUT=180                          # オプション、アイドル切断時間（秒）
```

## 起動

```bash
python main.py
```

### systemdサービスとして起動

```bash
sudo cp niconico-bot.service /etc/systemd/system/
sudo systemctl enable niconico-bot
sudo systemctl start niconico-bot
```

## 効果音

`sounds/na-.mp3` に効果音ファイルを配置してください。

## アーキテクチャ

- `main.py` - シングルファイル構成
- `GuildState` クラスでサーバーごとの状態を管理（キュー、VC、現在の曲、アイドルタスク）
- ニコニコ動画の音yt-dlpでローカルにダウンロードしてから再生（403エラー回避）
- YouTubeはストリーム再生
- 再生終了後に一時ファイルを自動削除
- CookieはAPIログインで取得、失敗時にSeleniumフォールバック

## GitOps

このプロジェクトは自動化されたGitOpsワークフローを使用しています：
- PR to master → Auto merge
- Merge to master → Version tag
- Daily 18:00 JST → Deploy to Raspberry Pi via Tailscale
