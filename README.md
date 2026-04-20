# INMERMUSIC_BOT

Discordでニコニコ動画を再生できるMusic Bot

## 機能

- ニコニコ動画・YouTubeの再生
- `/play` - 曲再生（URLまたは検索キーワード）
- `/skip` - スキップ
- `/queue` - キュー表示
- `/stop` - 停止
- `/pause` / `/resume` - 一時停止/再開
- `/nowplaying` - 再生中の曲表示
- `/na-` - 効果音再生（ンアッー!）
- `/refresh` - ニコニコCookie更新

## 必要環境

- Raspberry Pi 4 (aarch64)
- Python 3.11+
- discord.py
- yt-dlp
- Selenium + Chromium

## セットアップ

```bash
# 仮想環境作成
python3 -m venv venv
source venv/bin/activate

# 依存関係インストール
pip install -r requirements.txt

# 環境変数設定
cp .env.example .env
# .envファイルを編集してトークンを設定
```

## .env設定

```
DISCORD_TOKEN=あなたのDiscordBotトークン
COOKIE_FILE=cookies.txtへのパス
NICO_EMAIL=ニコニコメールアドレス
NICO_PASSWORD=ニコニコパスワード
```

## 起動

```bash
python main.py
```

またはsystemdサービスとして起動:

```bash
sudo cp niconico-bot.service /etc/systemd/system/
sudo systemctl enable niconico-bot
sudo systemctl start niconico-bot
```

## 効果音

`sounds/na-.mp3` に効果音ファイルを配置