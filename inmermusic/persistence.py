"""SQLite persistence for queues, history, favorites, and guild preferences."""
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

from . import config
from .config import FAVORITES_PAGE_SIZE, IDLE_TIMEOUT, logger

_db_lock = threading.RLock()
_SONG_FIELDS = {
    "url", "title", "duration", "thumbnail", "is_niconico", "needs_local",
    "uploader", "text_channel_id", "requester", "requester_id", "autoplay",
}


def _db_path() -> str:
    return os.path.join(config.STATE_DIR, "music.db")


class _SharedConnection(sqlite3.Connection):
    """A connection the callers may not actually close.

    Every accessor in this module is written as `conn = _connect()` / `try:` /
    `finally: conn.close()`. The connection is now process-wide, so close() has
    to be inert; `reset_connection()` is the only way to really drop it.
    """

    def close(self) -> None:  # noqa: D102 - see class docstring
        pass

    def _close_for_real(self) -> None:
        sqlite3.Connection.close(self)


_conn: Optional[_SharedConnection] = None
_conn_path: Optional[str] = None


def reset_connection() -> None:
    """Drop the cached connection (tests that repoint config.STATE_DIR)."""
    global _conn, _conn_path
    with _db_lock:
        if _conn is not None:
            try:
                _conn._close_for_real()
            except sqlite3.Error:
                pass
        _conn = None
        _conn_path = None


def _connect() -> sqlite3.Connection:
    """Return the process-wide connection, building it on first use.

    Reconnecting per call meant redoing makedirs + chmod + open + chmod +
    sqlite3.connect + `PRAGMA journal_mode=WAL` (itself a write) + 6 CREATE
    statements before every single query — all of it synchronously on the event
    loop, on a Raspberry Pi SD card. `_db_lock` (an RLock) already serializes
    every accessor, so one connection shared across threads is safe.

    `config.STATE_DIR` is a mutable module global (tests repoint it), so the
    cache is keyed on the resolved path and rebuilds itself when it moves.
    """
    global _conn, _conn_path
    with _db_lock:
        path = _db_path()
        if _conn is not None and _conn_path == path:
            return _conn
        reset_connection()
        _conn = _build_connection()
        _conn_path = path
        return _conn


def _build_connection() -> _SharedConnection:
    os.makedirs(config.STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.STATE_DIR, 0o700)
    path = _db_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(
        path, factory=_SharedConnection, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS queues (
            guild_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            song_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, position)
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            song_json TEXT NOT NULL,
            played_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS history_guild_time
            ON history(guild_id, played_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS favorites (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            song_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, url)
        );
        CREATE TABLE IF NOT EXISTS named_playlists (
            guild_id INTEGER NOT NULL,
            name_key TEXT NOT NULL,
            name TEXT NOT NULL,
            songs_json TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, name_key)
        );
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            default_volume INTEGER NOT NULL DEFAULT 100,
            idle_timeout INTEGER NOT NULL DEFAULT 180,
            loop_mode TEXT NOT NULL DEFAULT 'off',
            autoplay INTEGER NOT NULL DEFAULT 0,
            normalize INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS play_counts (
            guild_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            song_json TEXT NOT NULL,
            play_count INTEGER NOT NULL DEFAULT 0,
            total_sec INTEGER NOT NULL DEFAULT 0,
            last_played INTEGER NOT NULL,
            PRIMARY KEY (guild_id, url)
        );
        CREATE INDEX IF NOT EXISTS play_counts_rank
            ON play_counts(guild_id, play_count DESC, last_played DESC, url);
        CREATE TABLE IF NOT EXISTS requester_counts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            play_count INTEGER NOT NULL DEFAULT 0,
            total_sec INTEGER NOT NULL DEFAULT 0,
            last_played INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS backfill_state (
            guild_id INTEGER PRIMARY KEY,
            counted_from INTEGER NOT NULL
        );
        """
    )
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS can't add to an older DB."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(guild_settings)")
    }
    if "autoplay" not in columns:
        conn.execute(
            "ALTER TABLE guild_settings "
            "ADD COLUMN autoplay INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    if "normalize" not in columns:
        # Rows written before this column existed default to 0 (off), so an
        # existing guild's playback is unchanged until someone opts in.
        conn.execute(
            "ALTER TABLE guild_settings "
            "ADD COLUMN normalize INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(queues)")}
    if "updated_at" not in columns:
        # Rows written before this column existed get 0, i.e. "too old to
        # restore" — safer than resurrecting a queue of unknown age.
        conn.execute(
            "ALTER TABLE queues ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


_write_executor: Optional[ThreadPoolExecutor] = None


def _writer() -> ThreadPoolExecutor:
    global _write_executor
    with _db_lock:
        if _write_executor is None:
            # Exactly one worker: submissions run FIFO, so a later snapshot can
            # never be overwritten by an earlier one that finished late.
            _write_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="inmermusic-db")
        return _write_executor


def _log_write_error(future) -> None:
    error = future.exception()
    if error is not None:
        logger.warning(f"Background DB write failed: {error}")


def submit_write(fn, *args) -> None:
    """Run a fire-and-forget DB write off the event loop, in submission order.

    save_queue is a DELETE plus up to MAX_QUEUE_SIZE INSERTs and a commit
    (an fsync on the Pi's SD card), and it runs twice per track plus on every
    /shuffle, /remove, /move and /clear. Nothing reads its return value.
    """
    _writer().submit(fn, *args).add_done_callback(_log_write_error)


def flush_writes(timeout: float = 5.0) -> None:
    """Block until every queued write has finished (tests, shutdown)."""
    with _db_lock:
        executor = _write_executor
    if executor is None:
        return
    executor.submit(lambda: None).result(timeout=timeout)


def clean_song(song: Dict[str, Any]) -> Dict[str, Any]:
    """Return the durable, non-runtime portion of a song mapping."""
    clean = {key: song.get(key) for key in _SONG_FIELDS if key in song}
    clean.setdefault("title", "Unknown")
    clean.setdefault("url", "")
    clean.setdefault("duration", 0)
    clean.setdefault("thumbnail", "")
    clean.setdefault("needs_local", True)
    clean["local_file"] = None
    return clean


def _decode_song(raw: str) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("url") else None


def save_queue(guild_id: int, songs: Iterable[Dict[str, Any]]) -> bool:
    now = int(time.time())
    rows = [
        (guild_id, position, json.dumps(clean_song(song), ensure_ascii=False), now)
        for position, song in enumerate(songs)
    ]
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute("DELETE FROM queues WHERE guild_id = ?", (guild_id,))
                conn.executemany(
                    "INSERT INTO queues (guild_id, position, song_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to persist queue for guild {guild_id}: {e}")
        return False


def load_queue(guild_id: int,
               max_age: Optional[float] = None) -> List[Dict[str, Any]]:
    """Load the persisted queue, optionally ignoring stale snapshots.

    A queue now survives a non-explicit disconnect, so without an age bound a
    week-old queue could suddenly start playing on the next /join.
    """
    try:
        with _db_lock:
            conn = _connect()
            try:
                if max_age is None:
                    rows = conn.execute(
                        "SELECT song_json FROM queues WHERE guild_id = ? "
                        "ORDER BY position",
                        (guild_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT song_json FROM queues "
                        "WHERE guild_id = ? AND updated_at >= ? ORDER BY position",
                        (guild_id, int(time.time() - max_age)),
                    ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to restore queue for guild {guild_id}: {e}")
        return []
    return [song for (raw,) in rows if (song := _decode_song(raw)) is not None]


def _duration_sec(song: Dict[str, Any]) -> int:
    """Best-effort seconds for a track; unknown/garbage durations count as 0."""
    try:
        return max(0, int(float(song.get("duration") or 0)))
    except (TypeError, ValueError):
        return 0


def _bump_play_counts(
    conn: sqlite3.Connection,
    guild_id: int,
    song: Dict[str, Any],
    payload: str,
    played_at: int,
) -> None:
    """Accumulate lifetime play totals. Runs inside record_history's transaction.

    Unlike `history` (rolling `limit` rows), these totals are never trimmed.

    Every field that describes "the most recent play" is guarded by the row's
    own `last_played` rather than taking `excluded` unconditionally, because
    /statsbackfill replays *old* plays through here: an unguarded assignment
    would rewind `last_played` (which /stats both displays and sorts on) and
    overwrite the current title/song_json with a stale copy. On the live path
    `excluded.last_played` is always the newest value, so the guards are no-ops
    there and the behaviour is unchanged.
    """
    url = song.get("url")
    if not url:
        return
    seconds = _duration_sec(song)
    conn.execute(
        "INSERT INTO play_counts "
        "(guild_id, url, title, song_json, play_count, total_sec, last_played) "
        "VALUES (?, ?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(guild_id, url) DO UPDATE SET "
        "play_count = play_count + 1, "
        "total_sec = total_sec + excluded.total_sec, "
        "title = CASE WHEN excluded.last_played >= last_played "
        "THEN excluded.title ELSE title END, "
        "song_json = CASE WHEN excluded.last_played >= last_played "
        "THEN excluded.song_json ELSE song_json END, "
        "last_played = MAX(last_played, excluded.last_played)",
        (guild_id, url, song.get("title") or url, payload, seconds, played_at),
    )
    try:
        user_id = int(song.get("requester_id"))
    except (TypeError, ValueError):
        return
    conn.execute(
        "INSERT INTO requester_counts "
        "(guild_id, user_id, name, play_count, total_sec, last_played) "
        "VALUES (?, ?, ?, 1, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "play_count = play_count + 1, "
        "total_sec = total_sec + excluded.total_sec, "
        "name = CASE WHEN excluded.name != '' AND excluded.last_played >= last_played "
        "THEN excluded.name ELSE name END, "
        "last_played = MAX(last_played, excluded.last_played)",
        (guild_id, user_id, str(song.get("requester") or ""), seconds, played_at),
    )


def record_history(guild_id: int, song: Dict[str, Any], limit: int = 200) -> bool:
    try:
        cleaned = clean_song(song)
        payload = json.dumps(cleaned, ensure_ascii=False)
        played_at = int(time.time())
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO history (guild_id, song_json, played_at) VALUES (?, ?, ?)",
                    (guild_id, payload, played_at),
                )
                conn.execute(
                    "DELETE FROM history WHERE guild_id = ? AND id NOT IN "
                    "(SELECT id FROM history WHERE guild_id = ? "
                    "ORDER BY played_at DESC, id DESC LIMIT ?)",
                    (guild_id, guild_id, limit),
                )
                _bump_play_counts(conn, guild_id, cleaned, payload, played_at)
                conn.commit()
            finally:
                conn.close()
        return True
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to persist history for guild {guild_id}: {e}")
        return False


def load_history(guild_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT song_json, played_at FROM history WHERE guild_id = ? "
                    "ORDER BY played_at DESC, id DESC LIMIT ?",
                    (guild_id, limit),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read history for guild {guild_id}: {e}")
        return []
    result = []
    for raw, played_at in rows:
        song = _decode_song(raw)
        if song is not None:
            song["played_at"] = played_at
            result.append(song)
    return result


def pop_history(guild_id: int) -> Optional[Dict[str, Any]]:
    """Remove and return the most recently played track atomically."""
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT id, song_json, played_at FROM history "
                    "WHERE guild_id = ? ORDER BY played_at DESC, id DESC LIMIT 1",
                    (guild_id,),
                ).fetchone()
                if row is None:
                    return None
                conn.execute("DELETE FROM history WHERE id = ?", (row[0],))
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to pop history for guild {guild_id}: {e}")
        return None
    song = _decode_song(row[1])
    if song is not None:
        song["played_at"] = row[2]
    return song


def _read_backfill_floor(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        "SELECT counted_from FROM backfill_state WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    return int(row[0]) if row else config.PLAY_COUNTS_EPOCH


def _lower_backfill_floor(
    conn: sqlite3.Connection, guild_id: int, counted_from: int
) -> None:
    """Extend the counted window backwards. The floor only ever moves down —
    raising it would re-open an already-counted stretch to a second pass."""
    conn.execute(
        "INSERT INTO backfill_state (guild_id, counted_from) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "counted_from = MIN(counted_from, excluded.counted_from)",
        (guild_id, int(counted_from)),
    )


def backfill_floor(guild_id: int) -> int:
    """Every play at/after this instant is already in `play_counts`.

    Defaults to `config.PLAY_COUNTS_EPOCH` — the moment the stats tables
    shipped, before which nothing was ever counted. /statsbackfill only ever
    reads plays strictly older than the floor, which is what keeps a second run
    (or a second source) from counting the same play twice.
    """
    try:
        with _db_lock:
            conn = _connect()
            try:
                return _read_backfill_floor(conn, guild_id)
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read backfill floor for guild {guild_id}: {e}")
        return config.PLAY_COUNTS_EPOCH


def backfill_from_history(guild_id: int) -> Dict[str, Any]:
    """Fold the `history` rows that predate the floor into `play_counts`.

    `history` is a rolling window (see `record_history`), so this reaches back
    at most `limit` tracks — but unlike the Discord scan it carries
    `requester_id`, so it is the only source that can also repair the DJ
    ranking.
    """
    result: Dict[str, Any] = {
        "plays": 0, "tracks": 0, "skipped": 0,
        "floor_before": config.PLAY_COUNTS_EPOCH, "floor_after": None,
    }
    try:
        with _db_lock:
            conn = _connect()
            try:
                floor = _read_backfill_floor(conn, guild_id)
                result["floor_before"] = floor
                result["floor_after"] = floor
                rows = conn.execute(
                    "SELECT song_json, played_at FROM history "
                    "WHERE guild_id = ? AND played_at < ? "
                    "ORDER BY played_at ASC, id ASC",
                    (guild_id, floor),
                ).fetchall()
                if not rows:
                    return result
                urls = set()
                new_floor = floor
                for raw, played_at in rows:
                    song = _decode_song(raw)
                    if song is None:
                        result["skipped"] += 1
                        continue
                    played_at = int(played_at)
                    _bump_play_counts(conn, guild_id, song, raw, played_at)
                    result["plays"] += 1
                    urls.add(song["url"])
                    # played_at is stamped when the track *finished*, so the
                    # play itself began roughly a duration earlier. Push the
                    # floor back to that start, or the Discord scan — which
                    # keys off the message's post time — would re-count this
                    # very track.
                    new_floor = min(new_floor, played_at - _duration_sec(song))
                if result["plays"]:
                    _lower_backfill_floor(conn, guild_id, new_floor)
                    result["floor_after"] = new_floor
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to backfill history for guild {guild_id}: {e}")
        return result
    result["tracks"] = len(urls)
    return result


def backfill_plays(
    guild_id: int,
    entries: Iterable[Dict[str, Any]],
    covered_from: int,
) -> Dict[str, Any]:
    """Fold past plays reconstructed outside the database into `play_counts`.

    `entries` are `{"song": <track dict>, "played_at": <epoch>}` — today, the
    now-playing embeds still sitting in a guild's text channels.

    `covered_from` is the oldest instant the caller can vouch for having looked
    at *everywhere* it needed to look, and only plays inside
    `[covered_from, floor)` are counted; anything older is dropped even when
    the caller already found it. That is what makes a partial scan safe to
    repeat: the floor advances to exactly the window that was fully covered, so
    a deeper second run picks the leftovers up instead of counting the shallow
    ones twice.
    """
    result: Dict[str, Any] = {
        "plays": 0, "tracks": 0, "skipped": 0,
        "floor_before": config.PLAY_COUNTS_EPOCH, "floor_after": None,
    }
    covered_from = int(covered_from)
    try:
        with _db_lock:
            conn = _connect()
            try:
                floor = _read_backfill_floor(conn, guild_id)
                result["floor_before"] = floor
                result["floor_after"] = floor
                if covered_from >= floor:
                    return result
                urls = set()
                for entry in entries:
                    song = entry.get("song") or {}
                    if not song.get("url"):
                        result["skipped"] += 1
                        continue
                    try:
                        played_at = int(entry["played_at"])
                    except (KeyError, TypeError, ValueError):
                        result["skipped"] += 1
                        continue
                    if not covered_from <= played_at < floor:
                        result["skipped"] += 1
                        continue
                    cleaned = clean_song(song)
                    payload = json.dumps(cleaned, ensure_ascii=False)
                    _bump_play_counts(conn, guild_id, cleaned, payload, played_at)
                    result["plays"] += 1
                    urls.add(cleaned["url"])
                _lower_backfill_floor(conn, guild_id, covered_from)
                result["floor_after"] = covered_from
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to backfill plays for guild {guild_id}: {e}")
        return result
    result["tracks"] = len(urls)
    return result


def top_songs(guild_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Most-played tracks, ranked. `song` holds the replayable track dict."""
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT url, title, song_json, play_count, total_sec, last_played "
                    "FROM play_counts WHERE guild_id = ? "
                    "ORDER BY play_count DESC, last_played DESC, url ASC LIMIT ?",
                    (guild_id, max(1, limit)),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read play counts for guild {guild_id}: {e}")
        return []
    result = []
    for url, title, raw, play_count, total_sec, last_played in rows:
        song = _decode_song(raw) or {"url": url, "title": title}
        song.setdefault("url", url)
        song.setdefault("title", title)
        result.append({
            "url": url,
            "title": title,
            "song": song,
            "play_count": play_count,
            "total_sec": total_sec,
            "last_played": last_played,
        })
    return result


def top_requesters(guild_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT user_id, name, play_count, total_sec, last_played "
                    "FROM requester_counts WHERE guild_id = ? "
                    "ORDER BY play_count DESC, last_played DESC, user_id ASC LIMIT ?",
                    (guild_id, max(1, limit)),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read requester counts for guild {guild_id}: {e}")
        return []
    return [
        {
            "user_id": user_id,
            "name": name,
            "play_count": play_count,
            "total_sec": total_sec,
            "last_played": last_played,
        }
        for user_id, name, play_count, total_sec, last_played in rows
    ]


def guild_play_totals(guild_id: int) -> Dict[str, int]:
    empty = {"unique_tracks": 0, "plays": 0, "total_sec": 0}
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(play_count), 0), "
                    "COALESCE(SUM(total_sec), 0) FROM play_counts WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read play totals for guild {guild_id}: {e}")
        return empty
    if not row:
        return empty
    return {"unique_tracks": int(row[0]), "plays": int(row[1]), "total_sec": int(row[2])}


def user_play_stats(guild_id: int, user_id: int) -> Dict[str, Any]:
    """One requester's totals plus their own most-played tracks."""
    empty: Dict[str, Any] = {"play_count": 0, "total_sec": 0, "songs": []}
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT play_count, total_sec FROM requester_counts "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                rows = conn.execute(
                    "SELECT song_json, played_at FROM history WHERE guild_id = ? "
                    "ORDER BY played_at DESC, id DESC LIMIT 200",
                    (guild_id,),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read user stats for guild {guild_id}: {e}")
        return empty
    if not row:
        return empty
    # play_counts is keyed by track, not requester, so per-user favourites can
    # only come from the (rolling) history rows.
    tally: Dict[str, Dict[str, Any]] = {}
    for raw, _played_at in rows:
        song = _decode_song(raw)
        if song is None or not song.get("url"):
            continue
        try:
            if int(song.get("requester_id")) != user_id:
                continue
        except (TypeError, ValueError):
            continue
        entry = tally.setdefault(
            song["url"],
            {"title": song.get("title") or song["url"], "play_count": 0},
        )
        entry["play_count"] += 1
    songs = sorted(
        tally.values(), key=lambda item: (-item["play_count"], item["title"]))
    return {
        "play_count": int(row[0]),
        "total_sec": int(row[1]),
        "songs": songs[:5],
    }


def _playlist_key(name: str) -> str:
    return name.strip().casefold()


def count_named_playlists(guild_id: int) -> int:
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM named_playlists WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to count playlists for guild {guild_id}: {e}")
        return 0
    return int(row[0]) if row else 0


def get_named_playlist_meta(guild_id: int, name: str) -> Optional[Dict[str, Any]]:
    """Owner and metadata for one playlist, or None when it does not exist."""
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT name, owner_id, updated_at FROM named_playlists "
                    "WHERE guild_id = ? AND name_key = ?",
                    (guild_id, _playlist_key(name)),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read playlist meta for guild {guild_id}: {e}")
        return None
    if row is None:
        return None
    return {"name": row[0], "owner_id": row[1], "updated_at": row[2]}


def save_named_playlist(
    guild_id: int, name: str, songs: Iterable[Dict[str, Any]], owner_id: int,
    force: bool = False,
) -> str:
    """Create or overwrite a playlist. Returns "saved", "denied", or "error".

    Overwriting an existing playlist requires `owner_id` to match the stored
    owner (or `force`, for Manage Guild). The ownership test lives in the SQL
    itself, so a caller's pre-check can't be raced, and `owner_id` is never
    updated on conflict — overwriting must not transfer ownership.
    """
    payload = json.dumps([clean_song(song) for song in songs], ensure_ascii=False)
    try:
        with _db_lock:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "INSERT INTO named_playlists "
                    "(guild_id, name_key, name, songs_json, owner_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, name_key) DO UPDATE SET "
                    "name = excluded.name, songs_json = excluded.songs_json, "
                    "updated_at = excluded.updated_at "
                    "WHERE ? = 1 OR named_playlists.owner_id = excluded.owner_id",
                    (
                        guild_id, _playlist_key(name), name.strip(), payload,
                        owner_id, int(time.time()), 1 if force else 0,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to save playlist for guild {guild_id}: {e}")
        return "error"
    # A suppressed DO UPDATE reports zero changes: someone else owns the name.
    return "saved" if cursor.rowcount > 0 else "denied"


def load_named_playlist(guild_id: int, name: str) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT songs_json FROM named_playlists "
                    "WHERE guild_id = ? AND name_key = ?",
                    (guild_id, _playlist_key(name)),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to load playlist for guild {guild_id}: {e}")
        return []
    if row is None:
        return []
    try:
        songs = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return []
    return [
        clean_song(song) for song in songs
        if isinstance(song, dict) and song.get("url")
    ]


def list_named_playlists(guild_id: int) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT name, songs_json, owner_id, updated_at "
                    "FROM named_playlists WHERE guild_id = ? "
                    "ORDER BY updated_at DESC, name_key",
                    (guild_id,),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to list playlists for guild {guild_id}: {e}")
        return []
    result = []
    for name, raw, owner_id, updated_at in rows:
        try:
            count = len(json.loads(raw))
        except (TypeError, json.JSONDecodeError):
            count = 0
        result.append({
            "name": name, "song_count": count,
            "owner_id": owner_id, "updated_at": updated_at,
        })
    return result


def delete_named_playlist(
    guild_id: int, name: str, user_id: Optional[int] = None, force: bool = False,
) -> str:
    """Delete a playlist. Returns "deleted", "denied", "missing", or "error".

    `user_id` must match the stored owner unless `force` (Manage Guild) is set;
    the condition is part of the DELETE so a pre-check can't be raced. Passing
    neither `user_id` nor `force` deletes nothing.
    """
    key = _playlist_key(name)
    try:
        with _db_lock:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM named_playlists "
                    "WHERE guild_id = ? AND name_key = ? "
                    "AND (? = 1 OR owner_id = ?)",
                    (guild_id, key, 1 if force else 0, user_id),
                )
                conn.commit()
                if cursor.rowcount > 0:
                    return "deleted"
                exists = conn.execute(
                    "SELECT 1 FROM named_playlists "
                    "WHERE guild_id = ? AND name_key = ?",
                    (guild_id, key),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to delete playlist for guild {guild_id}: {e}")
        return "error"
    return "denied" if exists else "missing"


def add_favorite(guild_id: int, user_id: int, song: Dict[str, Any]) -> bool:
    clean = clean_song(song)
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO favorites (guild_id, user_id, url, song_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id, url) "
                    "DO UPDATE SET song_json = excluded.song_json, created_at = excluded.created_at",
                    (
                        guild_id, user_id, clean["url"],
                        json.dumps(clean, ensure_ascii=False), int(time.time()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to add favorite for guild {guild_id}: {e}")
        return False


def remove_favorite(guild_id: int, user_id: int, position: int) -> Optional[Dict[str, Any]]:
    # Same window as /favorite list, so `position` resolves to the row the user read.
    songs = load_favorites(guild_id, user_id, limit=FAVORITES_PAGE_SIZE)
    if not 1 <= position <= len(songs):
        return None
    song = songs[position - 1]
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "DELETE FROM favorites WHERE guild_id = ? AND user_id = ? AND url = ?",
                    (guild_id, user_id, song["url"]),
                )
                conn.commit()
            finally:
                conn.close()
        return song
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to remove favorite for guild {guild_id}: {e}")
        return None


def load_favorites(
    guild_id: int, user_id: int, limit: int = FAVORITES_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                # `created_at` is whole seconds, so favorites added in the same
                # second tie. Without the `url` tiebreaker SQLite may order ties
                # differently per query plan, and the number shown by /favorite list
                # would no longer address the same row as /favorite remove.
                rows = conn.execute(
                    "SELECT song_json FROM favorites WHERE guild_id = ? AND user_id = ? "
                    "ORDER BY created_at DESC, url LIMIT ?",
                    (guild_id, user_id, limit),
                ).fetchall()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read favorites for guild {guild_id}: {e}")
        return []
    return [song for (raw,) in rows if (song := _decode_song(raw)) is not None]


def get_settings(guild_id: int) -> Dict[str, Any]:
    defaults = {
        "default_volume": 100,
        "idle_timeout": IDLE_TIMEOUT,
        "loop_mode": "off",
        "autoplay": False,
        "normalize": False,
    }
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT default_volume, idle_timeout, loop_mode, autoplay, "
                    "normalize FROM guild_settings WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to read settings for guild {guild_id}: {e}")
        return defaults
    if not row:
        return defaults
    return {
        "default_volume": max(0, min(200, int(row[0]))),
        "idle_timeout": max(30, min(3600, int(row[1]))),
        "loop_mode": row[2] if row[2] in {"off", "song", "queue"} else "off",
        "autoplay": bool(row[3]),
        "normalize": bool(row[4]),
    }


def update_settings(guild_id: int, **changes: Any) -> Dict[str, Any]:
    settings = get_settings(guild_id)
    settings.update({key: value for key, value in changes.items() if value is not None})
    settings["default_volume"] = max(0, min(200, int(settings["default_volume"])))
    settings["idle_timeout"] = max(30, min(3600, int(settings["idle_timeout"])))
    if settings["loop_mode"] not in {"off", "song", "queue"}:
        settings["loop_mode"] = "off"
    settings["autoplay"] = bool(settings["autoplay"])
    settings["normalize"] = bool(settings["normalize"])
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO guild_settings "
                    "(guild_id, default_volume, idle_timeout, loop_mode, "
                    "autoplay, normalize) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET "
                    "default_volume = excluded.default_volume, "
                    "idle_timeout = excluded.idle_timeout, "
                    "loop_mode = excluded.loop_mode, autoplay = excluded.autoplay, "
                    "normalize = excluded.normalize",
                    (
                        guild_id, settings["default_volume"],
                        settings["idle_timeout"], settings["loop_mode"],
                        int(settings["autoplay"]), int(settings["normalize"]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to persist settings for guild {guild_id}: {e}")
    return settings


def delete_guild_data(guild_id: int) -> None:
    try:
        with _db_lock:
            conn = _connect()
            try:
                for table in (
                    "queues", "history", "favorites",
                    "named_playlists", "guild_settings",
                    "play_counts", "requester_counts", "backfill_state",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to delete persisted data for guild {guild_id}: {e}")
