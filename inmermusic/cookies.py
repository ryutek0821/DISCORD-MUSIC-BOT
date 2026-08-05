"""niconico session handling, guild session storage, and cookie persistence.

COOKIE_FILE is read via ``config.COOKIE_FILE`` so tests can monkeypatch it.

This module does **not** log in to niconico. niconico replaced its
server-rendered login form with an MFA-capable SPA, which removed
``POST /login/redirector`` (now 404) and every element id the old Selenium
fallback drove — both paths broke at once and neither is repairable in a way
that stays fixed. Authentication now comes from two places only: a
``user_session`` supplied by hand (globally via ``NICO_SESSION``, per guild via
``set_guild_session``), and yt-dlp's own niconico login (see
``audio.build_ydl_opts``), which is maintained upstream against site changes.
"""
import os
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from . import config
from .config import NICO_SESSION, logger

# Sessions are long-lived, but pin an explicit expiry so yt-dlp keeps sending
# the cookie rather than treating it as a session cookie it may drop.
SESSION_COOKIE_TTL = 180 * 24 * 60 * 60
# threading.Lock (not asyncio): cookie work runs in executor threads, so the
# foreground extract path must serialize there, not on the event loop.
# cookie_refresh_lock keeps two writers off the file at once; cookie_file_lock
# guards the atomic file write so a concurrent yt-dlp read never sees a partial.
cookie_refresh_lock = threading.Lock()
cookie_file_lock = threading.Lock()
# Per-guild RLocks keep one guild's DB value and generated file in sync without
# blocking other guilds. The short-lived cache lock only protects its mapping.
guild_session_locks_lock = threading.Lock()
guild_session_locks = {}
guild_cookie_cache_lock = threading.Lock()
guild_cookie_cache: Dict[int, tuple[str, str]] = {}

def write_netscape_cookies(records: List[Dict[str, Any]],
                           output_path: Optional[str] = None) -> int:
    """Atomically write cookie records to a Netscape-format cookie file.

    Each record needs name/value plus optional domain/path/secure/expiry. Writes
    go through a temp file + os.replace (atomic on POSIX) under cookie_file_lock,
    so a concurrent yt-dlp read or another writer never sees a half-written file.
    output_path defaults to config.COOKIE_FILE for backward compatibility.
    Returns the number of cookies written.
    """
    target_path = output_path if output_path is not None else config.COOKIE_FILE
    if not target_path:
        raise ValueError("Cookie file path is not configured")

    lines = ["# Netscape HTTP Cookie File\n"]
    for r in records:
        domain = r.get("domain") or ".nicovideo.jp"
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = r.get("path") or "/"
        secure = "TRUE" if r.get("secure") else "FALSE"
        expiry = str(int(r["expiry"])) if r.get("expiry") else "0"
        lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{r['name']}\t{r['value']}\n")

    target_dir = os.path.dirname(os.path.abspath(target_path))
    with cookie_file_lock:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".cookies_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    return len(records)


def _guild_db_path() -> str:
    return os.path.join(config.STATE_DIR, "guilds.db")


def _guild_session_lock(guild_id: int):
    with guild_session_locks_lock:
        lock = guild_session_locks.get(guild_id)
        if lock is None:
            lock = threading.RLock()
            guild_session_locks[guild_id] = lock
        return lock


def _connect_guild_db() -> sqlite3.Connection:
    os.makedirs(config.STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.STATE_DIR, 0o700)
    path = _guild_db_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS guild_sessions ("
            "guild_id INTEGER PRIMARY KEY, "
            "user_session TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
    except Exception:
        conn.close()
        raise
    return conn


def set_guild_session(guild_id: int, user_session: str) -> None:
    """Persist a guild session, propagating errors so the UI can report them."""
    with _guild_session_lock(guild_id):
        conn = _connect_guild_db()
        try:
            conn.execute(
                "INSERT INTO guild_sessions (guild_id, user_session, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "user_session = excluded.user_session, "
                "updated_at = excluded.updated_at",
                (guild_id, user_session, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


def get_guild_session(
    guild_id: int, *, suppress_errors: bool = True,
) -> Optional[str]:
    with _guild_session_lock(guild_id):
        try:
            conn = _connect_guild_db()
        except (OSError, sqlite3.Error) as e:
            if not suppress_errors:
                raise
            logger.warning(f"Guild session store is unavailable: {e}")
            return None
        try:
            row = conn.execute(
                "SELECT user_session FROM guild_sessions WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error as e:
            if not suppress_errors:
                raise
            logger.warning(f"Failed to read guild session: {e}")
            return None
        finally:
            conn.close()


def list_guild_sessions(
    *, suppress_errors: bool = True,
) -> List[Dict[str, int]]:
    """List non-secret guild session metadata for the local admin CLI."""
    try:
        conn = _connect_guild_db()
    except (OSError, sqlite3.Error) as e:
        if not suppress_errors:
            raise
        logger.warning(f"Guild session store is unavailable: {e}")
        return []
    try:
        rows = conn.execute(
            "SELECT guild_id, updated_at FROM guild_sessions ORDER BY guild_id"
        ).fetchall()
        return [
            {"guild_id": int(guild_id), "updated_at": int(updated_at)}
            for guild_id, updated_at in rows
        ]
    except sqlite3.Error as e:
        if not suppress_errors:
            raise
        logger.warning(f"Failed to list guild sessions: {e}")
        return []
    finally:
        conn.close()


def delete_guild_session(
    guild_id: int, *, suppress_errors: bool = True,
) -> bool:
    """Best-effort cleanup that never breaks a guild teardown path."""
    deleted = False
    with _guild_session_lock(guild_id):
        try:
            conn = _connect_guild_db()
            try:
                cursor = conn.execute(
                    "DELETE FROM guild_sessions WHERE guild_id = ?",
                    (guild_id,),
                )
                deleted = cursor.rowcount > 0
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            if not suppress_errors:
                raise
            logger.warning(f"Failed to delete guild session from store: {e}")

        try:
            cookie_path = os.path.join(
                config.STATE_DIR, f"cookies_{guild_id}.txt")
            with guild_cookie_cache_lock:
                guild_cookie_cache.pop(guild_id, None)
            with cookie_file_lock:
                try:
                    os.remove(cookie_path)
                except FileNotFoundError:
                    pass
        except Exception as e:
            if not suppress_errors:
                raise
            logger.warning(f"Failed to delete guild cookie file: {e}")
    return deleted


def guild_cookie_file(guild_id: int) -> Optional[str]:
    with _guild_session_lock(guild_id):
        user_session = get_guild_session(guild_id)
        if user_session is None:
            return None
        path = os.path.join(config.STATE_DIR, f"cookies_{guild_id}.txt")
        cache_value = (path, user_session)
        with guild_cookie_cache_lock:
            cached = guild_cookie_cache.get(guild_id)
        if cached == cache_value and os.path.exists(path):
            return path
        write_netscape_cookies(
            [{
                "domain": ".nicovideo.jp",
                "path": "/",
                "secure": True,
                "expiry": int(time.time()) + SESSION_COOKIE_TTL,
                "name": "user_session",
                "value": user_session,
            }],
            output_path=path,
        )
        with guild_cookie_cache_lock:
            guild_cookie_cache[guild_id] = cache_value
        return path


def cookie_file_has_session(path: Optional[str] = None) -> bool:
    """True when the cookie file already carries a user_session line."""
    target = path if path is not None else config.COOKIE_FILE
    if not target:
        return False
    try:
        with open(target) as f:
            return any(
                line.split("\t")[5] == "user_session"
                for line in f
                if not line.startswith("#") and len(line.split("\t")) >= 7
            )
    except OSError:
        return False


def ensure_nico_cookies(force: bool = False) -> bool:
    """Make sure COOKIE_FILE carries a usable global niconico user_session.

    Normally a no-op: yt-dlp owns COOKIE_FILE once a session is in it and
    writes refreshed cookies back on close, so overwriting on every extract
    would throw away the newer values. NICO_SESSION is therefore only written
    when the file has no session at all — or when ``force`` says to adopt a
    rotated NICO_SESSION (that is what /refresh now does).

    Returns whether a usable session ended up in the file.
    """
    if not config.COOKIE_FILE:
        logger.warning("COOKIE_FILE is not configured; niconico playback needs it")
        return False

    with cookie_refresh_lock:
        if not force and cookie_file_has_session():
            return True
        if NICO_SESSION:
            write_netscape_cookies([{
                "domain": ".nicovideo.jp",
                "path": "/",
                "secure": True,
                "expiry": int(time.time()) + SESSION_COOKIE_TTL,
                "name": "user_session",
                "value": NICO_SESSION,
            }])
            logger.info("Applied niconico user_session from NICO_SESSION")
            return True
        if cookie_file_has_session():
            # force=True with nothing to apply; keep whatever yt-dlp last wrote.
            return True
        ensure_cookie_file()
        logger.warning(
            "No niconico user_session available. Set NICO_SESSION in .env "
            "(copy the user_session cookie from a logged-in browser), or "
            "register one per guild. Falling back to yt-dlp's own login."
        )
        return False


def ensure_cookie_file() -> None:
    """Create an empty Netscape cookie file so yt-dlp can load and persist it."""
    if config.COOKIE_FILE and not os.path.exists(config.COOKIE_FILE):
        try:
            with open(config.COOKIE_FILE, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
            os.chmod(config.COOKIE_FILE, 0o600)
        except Exception as e:
            logger.warning(f"Failed to create cookie file: {e}")
