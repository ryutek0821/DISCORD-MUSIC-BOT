# AGENTS.md for INMERMUSIC_BOT

## Project Overview
Discord Music Bot for NicoNico/YouTube playback. Single-file Python app (`main.py`).

## Dev Commands
```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Run
python main.py
```

## Environment Variables (.env)
```
DISCORD_TOKEN=your_discord_bot_token_here
COOKIE_FILE=cookies.txt   # Netscape format cookie file
NICO_EMAIL=...
NICO_PASSWORD=...
```

## Architecture
- Single `main.py` with no test files, no type hints, no linter config
- discord.py 2.x with slash commands (`@bot.tree.command`)
- yt-dlp for media download
- Optional: Selenium + Chromium for cookie fallback

## Known Bugs (DO NOT IGNORE)
1. `resume_after_sound` (line ~295) is **undefined** - on_message sound effect breaks
2. `is_playing_sound[guild_id]` never reset to `False` in `on_message` - sound locked after first use
3. `message.guild` is `None` in DMs - crashes on DM messages
4. `song_start_time` saved but never used (line 214, 509)
5. `restart_song` doesn't check `vc` before calling `vc.play` (line 333)

## Critical Constraints
- No tests, no CI, no type checking
- Selenium fallback hardcoded path: `/usr/bin/chromedriver`
- Cookie TTL: 3600s hardcoded
- IDLE_TIMEOUT: 180s hardcoded
- Target: Raspberry Pi 4 (aarch64), Python 3.11+