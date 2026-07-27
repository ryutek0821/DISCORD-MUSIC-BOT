# AGENTS.md for INMERMUSIC_BOT

## Project Overview
Discord Music Bot for NicoNico/YouTube playback, written in Python with discord.py.

## Dev Commands
```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Lint
ruff check .

# Test
python tests/test_features.py

# Run
python main.py
```

## Environment Variables (.env)
```
DISCORD_TOKEN=your_discord_bot_token
COOKIE_FILE=cookies.txt
NICO_EMAIL=your_niconico_email
NICO_PASSWORD=your_niconico_password
```

## Architecture
Package `inmermusic/` with the following modules:
- `bot.py` — Bot startup (loads Cog, connects to Discord)
- `cog.py` — All slash command definitions (`discord.ext.commands.Cog`)
- `state.py` — `GuildState` dataclass (per-guild queue, VC, current track, idle task)
- `playback.py` — Queue progression, loop modes, skip logic
- `audio.py` — FFmpeg option builder, effect parameters, preset definitions
- `ui.py` — Now-playing Embed and control button View
- `cookies.py` — NicoNico cookie acquisition (API login + Selenium fallback)
- `config.py` — `Settings` dataclass loaded from env vars
- `util.py` — Shared helpers

`main.py` is a thin entry point that calls `inmermusic.bot.run()`.

## Key Implementation Details
- NicoNico audio is downloaded locally via yt-dlp before playback (avoids 403 errors)
- YouTube uses streaming playback
- Temp files are deleted after each track ends
- Cookies are refreshed via NicoNico API; Selenium (Chromium) is the fallback
- All per-guild state lives in `GuildState`; concurrent guilds are fully isolated

## CI/CD
- `ci.yml`: ruff lint + test_features.py on every PR and push to master
- `auto-merge.yml`: squash-merges PRs when CI passes
- `deploy-on-push.yml`: rsync + systemctl restart to RYU-RASPBERRYPI via Tailscale SSH
- `version-tag.yml`: tags master commits with a version number

## エージェント運用ルール

`auto-merge.yml` + `deploy-on-push.yml` により、**PR を1本出すと人のレビューを経ずに本番 Bot が
差し替わる**（CI通過 → 自動 squash マージ → RYU-RASPBERRYPI へ rsync → `niconico-bot` 再起動）。

そのため Claude Code / Codex はこのリポジトリで PR を直接作らない。変更が必要だと判断したら
`.github/ISSUE_TEMPLATE/agent-proposal.yml` の形式で issue を起票し、そこで止まる
（実装・ブランチ作成・PR は人が明示的に指示してから）。
詳細は `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` の「GitHub 運用方針」を参照。
