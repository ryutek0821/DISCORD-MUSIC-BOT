"""Audio extraction/download (yt-dlp) and FFmpeg source building/seeking."""
import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict, List, NamedTuple, Optional

import discord
import yt_dlp

from . import config
from .config import (DOWNLOAD_TIMEOUT, EFFECT_FILTERS, MAX_PLAYLIST_SIZE,
                     MAX_TRACK_DURATION, NICO_EMAIL, NICO_PASSWORD,
                     SOURCE_CLEANUP_DELAY, logger)
from .cookies import (ensure_cookie_file, guild_cookie_file,
                      refresh_nico_cookies_sync)
from .state import GuildState

_GUILD_COOKIE_UNSET = object()
_PROXY_UNSET = object()
_proxy_lock = threading.Lock()
_preferred_proxy: Optional[str] = None


def _proxy_candidates(url: str) -> List[Optional[str]]:
    if _is_niconico_url(url):
        return [None]
    configured = list(config.YT_PROXIES)
    # Tests and long-running processes may update the legacy value after
    # import, so retain dynamic backward compatibility.
    if not configured and config.YT_PROXY:
        configured = [config.YT_PROXY]
    if not configured:
        return [None]
    with _proxy_lock:
        preferred = _preferred_proxy
    if preferred in configured:
        configured.remove(preferred)
        configured.insert(0, preferred)
    return configured


def _mark_proxy_success(proxy: Optional[str]) -> None:
    if proxy is None:
        return
    global _preferred_proxy
    with _proxy_lock:
        _preferred_proxy = proxy


def _redact_error(error: Any) -> str:
    text = str(error)
    for proxy in set(config.YT_PROXIES + ([config.YT_PROXY] if config.YT_PROXY else [])):
        text = text.replace(proxy, "<proxy>")
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def _proxy_retryable(error: Any) -> bool:
    text = str(error).lower()
    return any(token in text for token in (
        "403", "429", "timed out", "timeout", "connection reset",
        "connection refused", "unable to download", "sign in to confirm",
        "bot", "temporarily unavailable",
    ))


def build_ydl_opts(url: str, guild_id: Optional[int] = None,
                   _guild_cookie: Any = _GUILD_COOKIE_UNSET,
                   _proxy: Any = _PROXY_UNSET,
                   **overrides: Any) -> Dict[str, Any]:
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

    is_niconico = _is_niconico_url(url)
    if _guild_cookie is _GUILD_COOKIE_UNSET:
        guild_cookie = (guild_cookie_file(guild_id)
                        if is_niconico and guild_id is not None else None)
    else:
        guild_cookie = _guild_cookie

    if guild_cookie:
        ydl_opts["cookiefile"] = guild_cookie
    elif is_niconico and config.COOKIE_FILE:
        ensure_cookie_file()
        if os.path.exists(config.COOKIE_FILE):
            ydl_opts["cookiefile"] = config.COOKIE_FILE

    # Let yt-dlp perform the login itself using the current niconico flow.
    # It reuses the cached user_session cookie when present and saves fresh
    # cookies back to COOKIE_FILE on close.
    if is_niconico and not guild_cookie and NICO_EMAIL and NICO_PASSWORD:
        ydl_opts["username"] = NICO_EMAIL
        ydl_opts["password"] = NICO_PASSWORD

    # Send YouTube (and other non-niconico) requests through the selected
    # residential proxy. Callers performing retries pass an explicit proxy;
    # ordinary callers retain the legacy "first configured proxy" behavior.
    if not is_niconico and "proxy" not in ydl_opts:
        proxy = _proxy_candidates(url)[0] if _proxy is _PROXY_UNSET else _proxy
        if proxy:
            ydl_opts["proxy"] = proxy

    return ydl_opts


def _is_niconico_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "nicovideo.jp" or host.endswith(".nicovideo.jp") or host == "nico.ms"


def is_playlist_url(url: str) -> bool:
    """Return whether a supported URL clearly represents a whole playlist."""
    parsed = urlparse(url)
    path = parsed.path or ""
    query = parse_qs(parsed.query)
    list_values = query.get("list")
    if list_values:
        # A copied watch URL still represents the selected video. YouTube
        # radio/mix IDs (RD...) are open-ended and are intentionally skipped.
        if list_values[0].startswith("RD"):
            return False
        if "v" not in query:
            return True
    if path == "/playlist" or path.startswith("/playlist/"):
        return True
    return _is_niconico_url(url) and (
        "/mylist/" in path or "/series/" in path)


def validate_query(query: str) -> None:
    """Allow only supported media hosts; bare text remains a YouTube search."""
    if not query or not query.strip():
        raise ValueError("検索語を入力してください")
    if len(query) > 200:
        raise ValueError("検索語が長すぎます")
    parsed = urlparse(query)
    if not parsed.scheme or "://" not in query:
        return
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("対応していないURL形式です")
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = (
        host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")
        or host == "nicovideo.jp" or host.endswith(".nicovideo.jp") or host == "nico.ms"
    )
    if not allowed:
        raise ValueError("YouTubeまたはニコニコ動画のURLのみ利用できます")


def _select_audio_format(info: Dict[str, Any]) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    formats = info.get("formats", [])
    for codec in ("opus", "aac", "m4a"):
        for item in formats:
            if item.get("acodec") != "none" and item.get("vcodec") == "none":
                if codec in (item.get("acodec", "") or item.get("ext", "")):
                    return item.get("url"), item
    for item in formats:
        if item.get("acodec") != "none" and item.get("vcodec") == "none":
            return item.get("url"), item
    for item in formats:
        if item.get("url"):
            return item.get("url"), item
    return None, None


def _canonical_url(info: Dict[str, Any], fallback: str) -> str:
    webpage_url = info.get("webpage_url") or info.get("original_url")
    if webpage_url:
        return webpage_url
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    video_id = info.get("id")
    raw_url = info.get("url")
    if video_id and ("youtube" in extractor or "youtu" in fallback):
        return f"https://www.youtube.com/watch?v={video_id}"
    if isinstance(raw_url, str) and raw_url.startswith(("http://", "https://")):
        return raw_url
    return fallback


def _flat_entry_url(info: Dict[str, Any], source_url: str) -> Optional[str]:
    """Return a concrete media URL for a flat search/playlist entry.

    Flat yt-dlp entries occasionally contain only an extractor-specific ID.
    Never fall back to the search or playlist URL itself: doing that can make
    a malformed row replay the first search result or the wrong playlist item.
    """
    for key in ("webpage_url", "url"):
        value = info.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    video_id = info.get("id")
    if not video_id:
        return None
    extractor = (
        f"{info.get('extractor_key', '')} {info.get('extractor', '')}"
    ).lower()
    if "nico" in extractor or _is_niconico_url(source_url):
        return f"https://www.nicovideo.jp/watch/{video_id}"
    if (
        "youtube" in extractor
        or source_url.startswith("ytsearch")
        or "youtu" in (urlparse(source_url).hostname or "")
    ):
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def _song_from_info(info: Dict[str, Any], fallback_url: str,
                    *, require_audio: bool = False) -> Dict[str, Any]:
    audio_url, selected_format = _select_audio_format(info)
    if require_audio and not audio_url:
        raise ValueError("No audio URL found in extracted info")
    if selected_format:
        logger.info(
            "Selected audio format: %s (%skbps, %sHz)",
            selected_format.get("acodec", "unknown"),
            selected_format.get("abr", "unknown"),
            selected_format.get("asr", "unknown"),
        )
    webpage_url = _canonical_url(info, fallback_url)
    extractor = (info.get("extractor_key") or info.get("extractor") or "")
    is_niconico = _is_niconico_url(webpage_url)
    is_youtube = "youtube" in extractor.lower() or "youtube.com" in webpage_url
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration and duration > MAX_TRACK_DURATION:
        raise ValueError("動画が長すぎます")
    return {
        "url": webpage_url,
        "audio_url": audio_url,
        "title": info.get("title") or "Unknown",
        "duration": duration,
        "thumbnail": info.get("thumbnail", ""),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "is_niconico": is_niconico,
        "needs_local": is_niconico or is_youtube,
        "local_file": None,
    }


def _extract_info_with_failover(url: str, guild_id: Optional[int],
                                **overrides: Any) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    proxies = _proxy_candidates(url)
    for index, proxy in enumerate(proxies):
        try:
            opts = build_ydl_opts(url, guild_id=guild_id, _proxy=proxy, **overrides)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            _mark_proxy_success(proxy)
            return info
        except Exception as e:
            last_error = e
            if index + 1 >= len(proxies) or not _proxy_retryable(e):
                break
            logger.warning("Media extraction failed via proxy #%d; trying next proxy", index + 1)
    assert last_error is not None
    raise last_error


def extract_audio_url(url: str, guild_id: Optional[int] = None) -> Dict[str, Any]:
    """Extract one playable track, with YouTube proxy failover."""
    validate_query(url)
    is_niconico = _is_niconico_url(url)
    guild_cookie = (guild_cookie_file(guild_id)
                    if is_niconico and guild_id is not None else None)
    if is_niconico and not guild_cookie:
        refresh_nico_cookies_sync()

    try:
        info = _extract_info_with_failover(
            url, guild_id, _guild_cookie=guild_cookie, socket_timeout=10)
        if isinstance(info, dict) and info.get("entries"):
            info = info["entries"][0]
        return _song_from_info(info, url, require_audio=True)
    except Exception as e:
        logger.error(f"Failed to extract audio URL: {_redact_error(e)}")
        raise


def search_candidates(query: str, guild_id: Optional[int] = None,
                      limit: int = 5) -> List[Dict[str, Any]]:
    """Return lightweight YouTube search choices without downloading audio."""
    validate_query(query)
    if "://" in query:
        return [extract_audio_url(query, guild_id)]
    count = max(1, min(25, int(limit)))
    search_url = f"ytsearch{count}:{query.strip()}"
    try:
        info = _extract_info_with_failover(
            search_url, guild_id, noplaylist=False, extract_flat="in_playlist",
            playlistend=count, socket_timeout=10)
        entries = info.get("entries", []) if isinstance(info, dict) else []
        songs: List[Dict[str, Any]] = []
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            entry_url = _flat_entry_url(entry, search_url)
            if entry_url is None:
                continue
            try:
                songs.append(_song_from_info(entry, entry_url))
            except ValueError:
                continue
        return songs
    except Exception as e:
        logger.error(f"Failed to search media: {_redact_error(e)}")
        raise


def extract_playlist(url: str, guild_id: Optional[int] = None,
                     limit: int = MAX_PLAYLIST_SIZE) -> List[Dict[str, Any]]:
    """Extract a bounded playlist as lightweight, lazily-downloaded songs."""
    validate_query(url)
    if "://" not in url:
        raise ValueError("プレイリストURLを指定してください")
    count = max(1, min(MAX_PLAYLIST_SIZE, int(limit)))
    is_niconico = _is_niconico_url(url)
    guild_cookie = (
        guild_cookie_file(guild_id)
        if is_niconico and guild_id is not None else None
    )
    if is_niconico and not guild_cookie:
        refresh_nico_cookies_sync()
    try:
        info = _extract_info_with_failover(
            url, guild_id, noplaylist=False, extract_flat="in_playlist",
            playlistend=count, socket_timeout=10,
            _guild_cookie=guild_cookie)
        entries = info.get("entries", []) if isinstance(info, dict) else []
        songs: List[Dict[str, Any]] = []
        for entry in entries[:count]:
            if not isinstance(entry, dict):
                continue
            entry_url = _flat_entry_url(entry, url)
            if entry_url is None:
                continue
            try:
                songs.append(_song_from_info(entry, entry_url))
            except ValueError:
                continue
        return songs
    except Exception as e:
        logger.error(f"Failed to extract playlist: {_redact_error(e)}")
        raise


class DownloadResult(NamedTuple):
    """Outcome of a download, including a safe user-facing failure source."""

    path: Optional[str]
    error: Optional[str] = None


def download_audio(url: str, guild_id: Optional[int] = None) -> DownloadResult:
    """Download audio to a fresh per-request temp directory.

    Used for niconico and YouTube; for YouTube the request is routed through
    the residential proxy (via build_ydl_opts) so the IP-locked media URL is
    fetched from the same IP that extracted it.

    Each call gets its own `dl_*` directory (instead of a uuid-suffixed
    filename directly in the shared temp dir) so a timed-out caller can find
    and remove the whole directory once the download eventually finishes —
    see `cleanup_download` and the callers in playback.py.
    """
    proxies = _proxy_candidates(url)
    for index, proxy in enumerate(proxies):
        tmpdir = tempfile.mkdtemp(prefix="dl_", dir=config.DOWNLOAD_DIR)
        output_template = os.path.join(tmpdir, "%(id)s.%(ext)s")
        ydl_opts = build_ydl_opts(
            url, guild_id=guild_id, _proxy=proxy, outtmpl=output_template,
            socket_timeout=min(30, DOWNLOAD_TIMEOUT))
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                if os.path.exists(filename):
                    _mark_proxy_success(proxy)
                    logger.info(f"Downloaded audio: {filename}")
                    return DownloadResult(filename)
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            if index + 1 < len(proxies) and _proxy_retryable(e):
                logger.warning("Audio download failed via proxy #%d; trying next proxy", index + 1)
                continue
            safe_error = _redact_error(e)
            logger.error(f"Failed to download audio: {safe_error}")
            return DownloadResult(None, safe_error)
        shutil.rmtree(tmpdir, ignore_errors=True)
    return DownloadResult(None, "downloaded file missing")


def cleanup_download(path: Optional[str]) -> None:
    """Remove a downloaded track's temp directory (see `download_audio`).

    Deletes the whole per-request `dl_*` directory rather than just the file,
    so nothing is left behind. Only ever touches a directory whose name
    actually starts with `dl_`, so a bad/unexpected path is a silent no-op
    rather than a footgun.
    """
    if not path:
        return
    parent = os.path.dirname(path)
    if os.path.basename(parent).startswith("dl_") and os.path.isdir(parent):
        shutil.rmtree(parent, ignore_errors=True)


def cleanup_temp_files(max_age: float = 3600) -> int:
    """Remove orphaned dl_* temp files/directories left by a previous crash.

    Downloads are normally deleted in the play `after` callback, but a crash
    mid-playback leaks them in the temp dir. Sweep ones older than max_age on
    startup so they don't accumulate. Handles both the current per-request
    `dl_*` directories and any pre-migration `dl_*` files. Returns the number
    removed.
    """
    removed = 0
    tmpdir = config.DOWNLOAD_DIR
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
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
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
    if state.clock_paused:
        return max(0.0, state.paused_position)
    if state.clock_started_at is not None:
        return max(0.0, state.clock_base +
                   (time.monotonic() - state.clock_started_at) * state.clock_speed)

    # Compatibility fallback for states created by older code/tests.
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
    state.clock_base = seek
    state.clock_speed = state.speed
    state.paused_position = seek
    state.clock_paused = was_paused
    state.clock_started_at = None if was_paused else time.monotonic()


def reapply_audio_settings(vc: discord.VoiceClient, state: GuildState) -> None:
    """Re-render the current song in place from the current playback position."""
    swap_source_at(vc, state, current_elapsed(vc, state))
