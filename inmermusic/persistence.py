"""SQLite persistence for queues, history, favorites, and guild preferences."""
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from . import config
from .config import IDLE_TIMEOUT, logger

_db_lock = threading.RLock()
_SONG_FIELDS = {
    "url", "title", "duration", "thumbnail", "is_niconico", "needs_local",
    "uploader", "text_channel_id", "requester", "requester_id",
}


def _db_path() -> str:
    return os.path.join(config.STATE_DIR, "music.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(config.STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.STATE_DIR, 0o700)
    path = _db_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS queues (
            guild_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            song_json TEXT NOT NULL,
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
            loop_mode TEXT NOT NULL DEFAULT 'off'
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
        """
    )
    return conn


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
    rows = [
        (guild_id, position, json.dumps(clean_song(song), ensure_ascii=False))
        for position, song in enumerate(songs)
    ]
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute("DELETE FROM queues WHERE guild_id = ?", (guild_id,))
                conn.executemany(
                    "INSERT INTO queues (guild_id, position, song_json) VALUES (?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to persist queue for guild {guild_id}: {e}")
        return False


def load_queue(guild_id: int) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT song_json FROM queues WHERE guild_id = ? ORDER BY position",
                    (guild_id,),
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
        "title = excluded.title, "
        "song_json = excluded.song_json, "
        "last_played = excluded.last_played",
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
        "name = CASE WHEN excluded.name != '' THEN excluded.name ELSE name END, "
        "last_played = excluded.last_played",
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


def save_named_playlist(
    guild_id: int, name: str, songs: Iterable[Dict[str, Any]], owner_id: int,
) -> bool:
    payload = json.dumps([clean_song(song) for song in songs], ensure_ascii=False)
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO named_playlists "
                    "(guild_id, name_key, name, songs_json, owner_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, name_key) DO UPDATE SET "
                    "name = excluded.name, songs_json = excluded.songs_json, "
                    "owner_id = excluded.owner_id, updated_at = excluded.updated_at",
                    (
                        guild_id, _playlist_key(name), name.strip(), payload,
                        owner_id, int(time.time()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to save playlist for guild {guild_id}: {e}")
        return False


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


def delete_named_playlist(guild_id: int, name: str) -> bool:
    try:
        with _db_lock:
            conn = _connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM named_playlists "
                    "WHERE guild_id = ? AND name_key = ?",
                    (guild_id, _playlist_key(name)),
                )
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to delete playlist for guild {guild_id}: {e}")
        return False
    return cursor.rowcount > 0


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
    songs = load_favorites(guild_id, user_id, limit=200)
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


def load_favorites(guild_id: int, user_id: int, limit: int = 25) -> List[Dict[str, Any]]:
    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT song_json FROM favorites WHERE guild_id = ? AND user_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
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
    }
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT default_volume, idle_timeout, loop_mode "
                    "FROM guild_settings WHERE guild_id = ?",
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
    }


def update_settings(guild_id: int, **changes: Any) -> Dict[str, Any]:
    settings = get_settings(guild_id)
    settings.update({key: value for key, value in changes.items() if value is not None})
    settings["default_volume"] = max(0, min(200, int(settings["default_volume"])))
    settings["idle_timeout"] = max(30, min(3600, int(settings["idle_timeout"])))
    if settings["loop_mode"] not in {"off", "song", "queue"}:
        settings["loop_mode"] = "off"
    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    "INSERT INTO guild_settings "
                    "(guild_id, default_volume, idle_timeout, loop_mode) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id) DO UPDATE SET "
                    "default_volume = excluded.default_volume, "
                    "idle_timeout = excluded.idle_timeout, loop_mode = excluded.loop_mode",
                    (
                        guild_id, settings["default_volume"],
                        settings["idle_timeout"], settings["loop_mode"],
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
                    "play_counts", "requester_counts",
                ):
                    conn.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
                conn.commit()
            finally:
                conn.close()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Failed to delete persisted data for guild {guild_id}: {e}")
