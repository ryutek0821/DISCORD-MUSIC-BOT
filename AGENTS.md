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
All previously listed bugs have been fixed. Current known issues:
- None at this time.

## Critical Constraints
- No tests, no CI, no type checking
- Selenium fallback hardcoded path: `/usr/bin/chromedriver`
- Cookie TTL: 3600s hardcoded
- IDLE_TIMEOUT: 180s hardcoded
- Target: Raspberry Pi 4 (aarch64), Python 3.11+