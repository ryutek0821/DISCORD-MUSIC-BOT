"""Audio extraction/download (yt-dlp) and FFmpeg source building/seeking."""
import asyncio
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import discord
import yt_dlp

from . import config
from .config import (DOWNLOAD_TIMEOUT, EFFECT_FILTERS, NICO_EMAIL, NICO_PASSWORD,
                     SOURCE_CLEANUP_DELAY, YT_PROXY, logger)
from .cookies import ensure_cookie_file, refresh_nico_cookies_sync
from .state import GuildState


def build_ydl_opts(url: str, **overrides: Any) -> Dict[str, Any]:
    """Build yt-dlp options, enabling niconico login + cookie persistence."""
    ydl_opts: Dict[str, Any] = {
        "format": "bestaudio[ext=opus]/bestaudio[ext=m4a]/bestaudio[ext=aac]/bestaudio/best",
        "format_sort": ["abr", "asr"],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Treat bare keywords as a YouTube search instead of an invalid URL.
        "default_search": "ytsearch",
    }
    ydl_opts.update(overrides)

    is_niconico = "nicovideo.jp" in url

    if config.COOKIE_FILE:
        ensure_cookie_file()
        if os.path.exists(config.COOKIE_FILE):
            ydl_opts["cookiefile"] = config.COOKIE_FILE

    # Let yt-dlp perform the login itself using the current niconico flow.
    # It reuses the cached user_session cookie when present and saves fresh
    # cookies back to COOKIE_FILE on close.
    if is_niconico and NICO_EMAIL and NICO_PASSWORD:
        ydl_opts["username"] = NICO_EMAIL
        ydl_opts["password"] = NICO_PASSWORD

    # Send YouTube (and other non-niconico) requests through the residential
    # proxy to dodge bot detection / 429 on the VPS datacenter IP.
    if not is_niconico and YT_PROXY and "proxy" not in ydl_opts:
        ydl_opts["proxy"] = YT_PROXY

    return ydl_opts


def extract_audio_url(url: str) -> Dict[str, Any]:
    """Extract audio stream URL or download for niconico."""
    if "nicovideo.jp" in url:
        refresh_nico_cookies_sync()

    ydl_opts = build_ydl_opts(url, socket_timeout=10)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if isinstance(info, dict) and "entries" in info and info["entries"]:
                info = info["entries"][0]

            formats = info.get("formats", [])
            audio_url = None
            selected_format = None

            for codec in ["opus", "aac", "m4a"]:
                for f in formats:
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        if codec in (f.get("acodec", "") or f.get("ext", "")):
                            audio_url = f.get("url")
                            selected_format = f
                            break
                if audio_url:
                    break

            if not audio_url:
                for f in formats:
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        selected_format = f
                        break

            if not audio_url:
                for f in formats:
                    if f.get("url"):
                        audio_url = f.get("url")
                        selected_format = f
                        break

            if not audio_url:
                raise ValueError("No audio URL found in extracted info")

            if selected_format:
                logger.info(f"Selected audio format: {selected_format.get('acodec', 'unknown')} "
                          f"({selected_format.get('abr', 'unknown')}kbps, "
                          f"{selected_format.get('asr', 'unknown')}Hz)")

            is_niconico = "nicovideo.jp" in url
            # A bare keyword resolves to a YouTube video via default_search, so
            # detect YouTube from the resolved info rather than the input string.
            extractor = (info.get("extractor_key") or info.get("extractor") or "")
            webpage_url = info.get("webpage_url") or url
            is_youtube = "youtube" in extractor.lower() or "youtube.com" in webpage_url

            # YouTube media URLs are IP-locked to the proxy used for extraction,
            # so they can't be streamed directly from the VPS. Fetch them to a
            # local file (through the proxy) at play time, like niconico.
            return {
                # Use the concrete video URL so the lazy download re-extracts the
                # exact video (and routes through the proxy via build_ydl_opts).
                "url": webpage_url if is_youtube else url,
                "audio_url": audio_url,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "is_niconico": is_niconico,
                "needs_local": is_niconico or is_youtube,
                "local_file": None,
            }
    except Exception as e:
        logger.error(f"Failed to extract audio URL: {e}")
        raise


def download_audio(url: str) -> Optional[str]:
    """Download audio to a temp file and return the path.

    Used for niconico and YouTube; for YouTube the request is routed through
    the residential proxy (via build_ydl_opts) so the IP-locked media URL is
    fetched from the same IP that extracted it.
    """
    tmpdir = tempfile.gettempdir()
    output_template = os.path.join(tmpdir, "dl_%(id)s.%(ext)s")

    # socket_timeout caps individual network reads so a dead connection raises
    # instead of blocking this executor thread forever (the async wait_for in
    # the callers only abandons the await, it can't kill the thread).
    ydl_opts = build_ydl_opts(url, outtmpl=output_template,
                              socket_timeout=min(30, DOWNLOAD_TIMEOUT))

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                logger.info(f"Downloaded audio: {filename}")
                return filename
    except Exception as e:
        logger.error(f"Failed to download audio: {e}")
    return None


def cleanup_temp_files(max_age: float = 3600) -> int:
    """Remove orphaned dl_* temp files left by a previous crash.

    Downloads are normally deleted in the play `after` callback, but a crash
    mid-playback leaks them in the temp dir. Sweep ones older than max_age on
    startup so they don't accumulate. Returns the number removed.
    """
    removed = 0
    tmpdir = tempfile.gettempdir()
    now = time.time()
    try:
        names = os.listdir(tmpdir)
    except OSError as e:
        logger.warning(f"Temp cleanup failed to list {tmpdir}: {e}")
        return 0
    for name in names:
        if not name.startswith("dl_"):
            continue
        path = os.path.join(tmpdir, name)
        try:
            if now - os.path.getmtime(path) > max_age:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(f"Cleaned up {removed} orphaned temp file(s)")
    return removed


def _atempo_chain(factor: float) -> List[str]:
    """Split a tempo factor into atempo filters within FFmpeg's 0.5–2.0 range."""
    parts: List[str] = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.6f}")
    return parts


def build_audio_filter(speed: float, pitch: int, volume: int = 100,
                       effect: str = "off") -> Optional[str]:
    """Build an FFmpeg -af value for tempo, pitch (semitones), volume and effect.

    Pitch uses the asetrate trick so it runs on the stock FFmpeg shipped with
    Raspberry Pi OS (no librubberband needed). Returns None when nothing applies.
    """
    filters: List[str] = []
    ratio = 2 ** (pitch / 12.0)
    if pitch != 0:
        # asetrate shifts pitch *and* speed by `ratio`; resample back to 48k.
        filters.append(f"asetrate={int(round(48000 * ratio))}")
        filters.append("aresample=48000")
    # Undo the asetrate speed change, then apply the requested speed.
    tempo = speed / ratio
    if abs(tempo - 1.0) > 1e-6:
        filters.extend(_atempo_chain(tempo))
    filters.extend(EFFECT_FILTERS.get(effect, []))
    if volume != 100:
        filters.append(f"volume={volume / 100:.3f}")
    return ",".join(filters) if filters else None


def make_audio_source(song: Dict[str, Any], state: GuildState, seek: float = 0.0) -> discord.FFmpegOpusAudio:
    """Build an Opus source honoring the guild's speed/pitch and an optional seek."""
    audio_source = song.get("local_file") or song.get("audio_url")
    is_local = bool(song.get("local_file"))

    before_parts: List[str] = []
    if not is_local:
        # -reconnect options only apply to network input; local files reject them.
        before_parts.append("-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
    if seek > 0:
        before_parts.append(f"-ss {seek:.3f}")
    before_options = " ".join(before_parts) if before_parts else None

    options = "-c:a libopus -b:a 192k -ar 48000 -ac 2"
    af = build_audio_filter(state.speed, state.pitch, state.volume, state.effect)
    if af:
        options += f' -af "{af}"'

    return discord.FFmpegOpusAudio(audio_source, before_options=before_options, options=options)


def current_elapsed(vc: discord.VoiceClient, state: GuildState) -> float:
    """Best-effort playback position (s) within the current song, seek-aware."""
    player = getattr(vc, "_player", None)
    loops = getattr(player, "loops", 0) if player else 0
    # Each 20ms output frame covers 0.02 * speed seconds of song content (atempo
    # time-stretches), so scale elapsed frames by the segment's playback speed.
    frames = (loops - state.loops_at_swap) * 0.02 * state.speed_at_swap
    return max(0.0, state.seek_position + frames)


def schedule_source_cleanup(source: discord.AudioSource) -> None:
    """Kill a hot-swapped FFmpeg process after a short delay.

    Cleaning up synchronously in swap_source_at can close the FFmpeg stdout pipe
    while the player thread is still blocked in old_source.read() — that read
    then returns b'' and discord.py stops playback. Easy to trigger when swaps
    arrive faster than FFmpeg emits its first packet (button mashing). Delaying
    the kill lets the player advance to the new source first.
    """
    def _kill() -> None:
        try:
            source.cleanup()
        except Exception as e:
            logger.warning(f"Failed to cleanup old audio source: {e}")

    try:
        loop = asyncio.get_running_loop()
        loop.call_later(SOURCE_CLEANUP_DELAY, lambda: loop.run_in_executor(None, _kill))
    except RuntimeError:
        # No running loop (shouldn't happen during playback) — clean up inline.
        _kill()


def swap_source_at(vc: discord.VoiceClient, state: GuildState, seek: float) -> None:
    """Hot-swap the FFmpeg source to restart the current song at `seek` seconds.

    Swapping vc.source (instead of vc.play) avoids firing the `after` callback,
    so the queue does not advance. The old source must be cleaned up manually.
    Used both to reapply speed/pitch/volume in place and to seek.
    """
    song = state.current_song
    if not song or not vc or not vc.is_connected():
        return
    was_paused = vc.is_paused()
    new_source = make_audio_source(song, state, seek=seek)
    old_source = vc.source
    vc.source = new_source  # _set_source resumes unconditionally; re-pause below
    if was_paused:
        vc.pause()
    if old_source:
        schedule_source_cleanup(old_source)
    player = getattr(vc, "_player", None)
    state.seek_position = seek
    state.loops_at_swap = getattr(player, "loops", 0) if player else 0
    state.speed_at_swap = state.speed


def reapply_audio_settings(vc: discord.VoiceClient, state: GuildState) -> None:
    """Re-render the current song in place from the current playback position."""
    swap_source_at(vc, state, current_elapsed(vc, state))
