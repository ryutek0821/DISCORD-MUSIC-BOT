"""niconico login, guild session storage, and Netscape cookie persistence.

COOKIE_FILE is read via ``config.COOKIE_FILE`` so tests can monkeypatch it.
"""
import os
import sqlite3
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from . import config
from .config import CHROMEDRIVER_PATH, COOKIE_TTL, NICO_EMAIL, NICO_PASSWORD, logger

last_cookie_refresh = 0
# threading.Lock (not asyncio): cookie refreshes run in executor threads, so the
# foreground extract path and the background loop must serialize there, not on
# the event loop. cookie_refresh_lock avoids duplicate logins; cookie_file_lock
# guards the atomic file write so a concurrent yt-dlp read never sees a partial.
cookie_refresh_lock = threading.Lock()
cookie_file_lock = threading.Lock()

NICO_LOGIN_BASE = "https://account.nicovideo.jp"


def login_via_api() -> requests.cookies.RequestsCookieJar:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    })
    # niconico deprecated /api/v1/login; the current login flow posts to
    # /login/redirector and sets the `user_session` cookie on success.
    session.get(f"{NICO_LOGIN_BASE}/login", timeout=(10, 30))
    resp = session.post(
        f"{NICO_LOGIN_BASE}/login/redirector",
        data={"mail_tel": NICO_EMAIL, "password": NICO_PASSWORD},
        headers={"Referer": f"{NICO_LOGIN_BASE}/login"},
        allow_redirects=True,
        timeout=(10, 30),
    )
    resp.raise_for_status()
    logger.info(f"API login status: {resp.status_code}")
    return session.cookies


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


def _connect_guild_db() -> sqlite3.Connection:
    os.makedirs(config.STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(config.STATE_DIR, 0o700)
    path = _guild_db_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    conn = sqlite3.connect(path)
    try:
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


def get_guild_session(guild_id: int) -> Optional[str]:
    try:
        conn = _connect_guild_db()
    except (OSError, sqlite3.Error) as e:
        logger.warning(f"Guild session store is unavailable: {e}")
        return None
    try:
        row = conn.execute(
            "SELECT user_session FROM guild_sessions WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.warning(f"Failed to read guild session: {e}")
        return None
    finally:
        conn.close()


def delete_guild_session(guild_id: int) -> None:
    conn = _connect_guild_db()
    try:
        conn.execute(
            "DELETE FROM guild_sessions WHERE guild_id = ?",
            (guild_id,),
        )
        conn.commit()
    finally:
        conn.close()
    cookie_path = os.path.join(config.STATE_DIR, f"cookies_{guild_id}.txt")
    with cookie_file_lock:
        try:
            os.remove(cookie_path)
        except FileNotFoundError:
            pass


def guild_cookie_file(guild_id: int) -> Optional[str]:
    user_session = get_guild_session(guild_id)
    if user_session is None:
        return None
    path = os.path.join(config.STATE_DIR, f"cookies_{guild_id}.txt")
    write_netscape_cookies(
        [{
            "domain": ".nicovideo.jp",
            "path": "/",
            "secure": True,
            "name": "user_session",
            "value": user_session,
        }],
        output_path=path,
    )
    return path


def save_session_cookies(cookies: requests.cookies.RequestsCookieJar) -> None:
    records = [
        {
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expiry": c.expires,
            "name": c.name,
            "value": c.value,
        }
        for c in cookies
    ]
    count = write_netscape_cookies(records)
    logger.info(f"Saved {count} session cookies")


def refresh_nico_cookies_sync(force: bool = False) -> bool:
    def _cache_valid() -> bool:
        return (not force
                and (time.time() - last_cookie_refresh) < COOKIE_TTL
                and bool(config.COOKIE_FILE)
                and os.path.exists(config.COOKIE_FILE)
                and os.path.getsize(config.COOKIE_FILE) > 0)

    if _cache_valid():
        logger.info("Using cached cookies (not expired)")
        return True

    if not config.COOKIE_FILE or not NICO_EMAIL or not NICO_PASSWORD:
        logger.warning("Niconico credentials/cookie path are not configured")
        return False

    # Serialize logins across the foreground extract path and the background
    # loop so we never run two logins (or two cookie writes) concurrently.
    with cookie_refresh_lock:
        if _cache_valid():
            logger.info("Using cached cookies (refreshed by another task)")
            return True
        return _do_refresh_nico_cookies()


def _do_refresh_nico_cookies() -> bool:
    """Run the actual niconico login (API, then Selenium fallback).

    The caller holds cookie_refresh_lock, so only one refresh runs at a time.
    """
    global last_cookie_refresh

    logger.info("Refreshing niconico cookies via API...")
    try:
        cookies = login_via_api()
        if any(c.name == "user_session" for c in cookies):
            save_session_cookies(cookies)
            last_cookie_refresh = time.time()
            return True
        logger.warning("API login did not return a user_session cookie")
    except Exception as e:
        logger.error(f"API login failed: {e}")

    logger.info("API login failed, trying Selenium fallback...")
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        service = Service(CHROMEDRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=options)

        try:
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            driver.get("https://account.nicovideo.jp/login?site=niconico")
            time.sleep(3)
            mail_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "input__mailtel"))
            )
            mail_field.send_keys(NICO_EMAIL)
            pass_field = driver.find_element(By.ID, "input__password")
            pass_field.send_keys(NICO_PASSWORD)
            driver.find_element(By.ID, "login__submit").click()
            time.sleep(10)

            cookies = driver.get_cookies()
            if not any(c.get("name") == "user_session" for c in cookies):
                raise RuntimeError("Selenium login did not return user_session")
            write_netscape_cookies(cookies)
            last_cookie_refresh = time.time()
            logger.info(f"Saved {len(cookies)} cookies via Selenium")
            return True
        finally:
            driver.quit()
    except Exception as e:
        logger.error(f"Selenium fallback failed: {e}")
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
