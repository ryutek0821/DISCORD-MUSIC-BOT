"""niconico login + Netscape cookie persistence.

COOKIE_FILE is read via ``config.COOKIE_FILE`` so tests can monkeypatch it.
"""
import os
import tempfile
import threading
import time
from typing import Any, Dict, List

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
    session.get(f"{NICO_LOGIN_BASE}/login")
    resp = session.post(
        f"{NICO_LOGIN_BASE}/login/redirector",
        data={"mail_tel": NICO_EMAIL, "password": NICO_PASSWORD},
        headers={"Referer": f"{NICO_LOGIN_BASE}/login"},
        allow_redirects=True,
    )
    logger.info(f"API login status: {resp.status_code}")
    return session.cookies


def write_netscape_cookies(records: List[Dict[str, Any]]) -> int:
    """Atomically write cookie records to COOKIE_FILE in Netscape format.

    Each record needs name/value plus optional domain/path/secure/expiry. Writes
    go through a temp file + os.replace (atomic on POSIX) under cookie_file_lock,
    so a concurrent yt-dlp read or another writer never sees a half-written file.
    Returns the number of cookies written.
    """
    lines = ["# Netscape HTTP Cookie File\n"]
    for r in records:
        domain = r.get("domain") or ".nicovideo.jp"
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".")
        path = r.get("path") or "/"
        secure = "TRUE" if r.get("secure") else "FALSE"
        expiry = str(int(r["expiry"])) if r.get("expiry") else "0"
        lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{r['name']}\t{r['value']}\n")

    target_dir = os.path.dirname(os.path.abspath(config.COOKIE_FILE))
    with cookie_file_lock:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".cookies_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, config.COOKIE_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    return len(records)


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
