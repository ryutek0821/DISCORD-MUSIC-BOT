# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Discord music bot (NicoNico + YouTube playback) written in Python with discord.py. Slash commands: `/play`, `/skip`, `/loop`, `/shuffle`, `/pitch`, `/volume`, `/preset`, `/na-` (soundboard).

## Dev Commands

```bash
# Setup (needs Python 3.11+; selenium>=4.40.0 won't install on older)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit values

# Lint
ruff check .

# Test (no Discord connection required)
python tests/test_features.py
# or: pytest tests/test_features.py

# Run
python main.py
```

There is a single test file (`tests/test_features.py`) with plain `assert`-based functions — run the whole file directly rather than filtering to one test, since it's fast and has no network/Discord dependency.

`ruff.toml` intentionally only selects `E9` (syntax errors) and `F` (pyflakes) — this is deliberate for the current single-package codebase, not an oversight. Don't "fix" it by broadening the rule set.

## Architecture

Package `inmermusic/`, layered bottom-up (each module's docstring states what it may import from):

- `config.py` — lowest layer, imports nothing from the package. Env vars, logging setup, and the effect/preset tables (`EFFECT_FILTERS`, `EFFECT_PRESETS`, `EFFECT_LABELS`, `EFFECT_EMOJI`) that drive both FFmpeg filter construction and the `/preset` UI dropdown — one source of truth, enforced by tests that every preset has a label/emoji.
- `state.py` — `GuildState` (per-guild queue, voice client, current track, speed/pitch/volume/effect, idle task, now-playing message/updater) plus the in-memory `guild_states` registry (`get_state(guild_id)`). Concurrent guilds are fully isolated.
- `cookies.py` — NicoNico cookie acquisition: primary path via NicoNico API login, Selenium (Chromium) fallback. `refresh_nico_cookies_sync` is serialized with a `threading.Lock`.
- `audio.py` — FFmpeg filter/option construction (`build_audio_filter`), audio source creation, download logic, temp-file cleanup.
- `util.py` — shared helpers (`parse_time`, `fmt_duration`, etc.), no package dependencies.
- `ui.py` — now-playing `Embed` builder and the persistent `MusicControls` button/dropdown `View`.
- `playback.py` — sits above `audio` + `ui`: queue advancement, loop modes, skip logic, idle-disconnect scheduling, and the now-playing progress-bar update loop.
- `cog.py` — all slash command definitions (`discord.ext.commands.Cog`), imports everything above.
- `bot.py` — bot construction and `run()` entrypoint; wires the cog, registers the persistent `MusicControls` view (so buttons survive a restart), starts the background cookie-refresh task.

`main.py` at the repo root is a thin entry point calling `inmermusic.bot.run()`.

### Key implementation details

- NicoNico audio is downloaded locally via yt-dlp before playback (streaming hits 403 errors); YouTube streams directly. Temp files are deleted after each track ends.
- `YT_PROXY` routes YouTube extraction/download through a residential-IP proxy when set, because the googlevideo media URLs are IP-locked to the extractor that requested them — both extraction and download must go through the same proxy.
- Effect/speed/pitch/volume changes are debounced (`EFFECT_DEBOUNCE`, config.py) so rapid button presses coalesce into a single FFmpeg source swap; the old FFmpeg process is killed after `SOURCE_CLEANUP_DELAY` seconds to avoid closing a pipe the player thread is still reading.
- Adding a new effect preset: add entries to `EFFECT_FILTERS`, `EFFECT_PRESETS`, `EFFECT_LABELS`, and `EFFECT_EMOJI` in `config.py` — the `/preset` slash command choices and now-playing dropdown are generated from these tables, no UI code changes needed.
- `sounds/*.mp3` (repo root) are the soundboard files served by `/na-`; `config.resolve_sound()` guards against path traversal in the name.
- `play_next` drains the queue in a `while` loop, not by recursion. It creates/announces the now-playing message **only after `vc.play()` succeeds**, so a failed track never leaves a stale (frozen) embed behind; each failure (download timeout/failure, play exception) calls `notify_skip()` and `continue`s to the next song. `announce=False` inverts ownership: the caller (`/play`) sends the now-playing followup itself from `state.current_song` after `play_next` returns. When adding a playback-failure branch, keep `notify_skip → continue` so a run of dead links can't wedge or silently drain the queue.
- All per-guild teardown goes through `playback.cleanup_guild_state(guild_id)` (cancels `idle_task`, `np_updater`, **and** `reapply_task`, clears `np_message`, drops the guild from `guild_states`). `/leave`, `/stop`, the idle disconnect, all-users-left in `on_voice_state_update`, and `on_guild_remove` all call it — don't hand-roll cleanup, or the debounced `reapply_task` leaks.
- `/play` extraction errors are humanized via `util.friendly_extract_error()` (pure token→Japanese mapping); `notify_skip` reasons stay generic because `download_audio` swallows its exception and returns `None`.

## CI/CD

- `ci.yml` — ruff + `tests/test_features.py`, runs on every PR and push to `master`.
- `auto-merge.yml` — squash-merges PRs automatically once CI passes.
- `deploy-on-push.yml` — on push to `master`, connects to Tailscale and `rsync`s the repo directly to `RYU-RASPBERRYPI` over its Tailscale IP, then restarts the `niconico-bot` systemd service. `.env`, `cookies.txt`, and `venv` on the server are preserved (excluded from rsync).
- `version-tag.yml` — tags `master` commits with a version number.

Because deploy runs automatically on every push to `master`, treat pushes to `master` as a production deploy trigger, not just a merge.
