"""Environment configuration, logging, and audio/effect constants.

Lowest layer: imports nothing from the package. COOKIE_FILE may be monkeypatched
in tests, so other modules read it as ``config.COOKIE_FILE`` (attribute access)
rather than importing the value by name.
"""
import os
import logging
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
COOKIE_FILE = os.getenv("COOKIE_FILE")
NICO_EMAIL = os.getenv("NICO_EMAIL")
NICO_PASSWORD = os.getenv("NICO_PASSWORD")
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH", "/usr/bin/chromedriver")
# Route YouTube traffic through a residential-IP proxy (e.g. a Tailscale RPi)
# so YouTube's datacenter-IP bot detection doesn't block extraction. The
# googlevideo media URLs are IP-locked to the extractor, so downloads must use
# the same proxy; that's why YouTube is fetched to a local file like niconico.
YT_PROXY = os.getenv("YT_PROXY")

# sounds/ lives at the repository root (one level above this package).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUNDS_DIR = os.path.join(_REPO_ROOT, "sounds")

if not os.path.isdir(SOUNDS_DIR):
    os.makedirs(SOUNDS_DIR, exist_ok=True)

# journald (systemd) captures stdout, so the stream handler is enough in
# production; set LOG_FILE to additionally keep a bounded, rotating log file.
_log_handlers: List[logging.Handler] = [logging.StreamHandler()]
LOG_FILE = os.getenv("LOG_FILE")
if LOG_FILE:
    from logging.handlers import RotatingFileHandler
    _log_handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("niconico-bot")

COOKIE_TTL = int(os.getenv("COOKIE_TTL", "3600"))

# Idle disconnect timeout (seconds) configurable via env
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "180"))

# Max seconds to wait for a single audio download before giving up, so a stalled
# fetch can't wedge the queue. Also passed to yt-dlp as socket_timeout (capped).
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "120"))

# How often (seconds) to edit the now-playing embed so the progress bar advances.
NP_UPDATE_INTERVAL = 10

# Coalesce a burst of effect/speed/pitch/volume changes (e.g. button mashing)
# into one FFmpeg source swap, fired this many seconds after the last change.
EFFECT_DEBOUNCE = 0.4
# Delay (seconds) before killing a hot-swapped FFmpeg process, so the player
# thread has moved to the new source and we never close a pipe it's reading.
SOURCE_CLEANUP_DELAY = 2.0

# Playback speed / pitch / volume limits shared by buttons and slash commands.
SPEED_MIN, SPEED_MAX, SPEED_STEP = 0.5, 2.0, 0.1
PITCH_MIN, PITCH_MAX = -12, 12
VOLUME_MIN, VOLUME_MAX, VOLUME_STEP = 0, 200, 20

# Extra FFmpeg filters layered on top of speed/pitch for each effect preset.
EFFECT_FILTERS: Dict[str, List[str]] = {
    "off": [],
    "nightcore": [],                       # tempo/pitch only (set via preset)
    "vaporwave": [],                       # tempo/pitch only (set via preset)
    "bassboost": ["bass=g=12"],
    "8d": ["apulsator=hz=0.09"],
    "lofi": ["lowpass=f=3200", "highpass=f=200"],
    "echo": ["aecho=0.8:0.9:1000:0.3"],
    "reverb": ["aecho=0.8:0.88:60:0.4"],
    "tremolo": ["tremolo=f=6:d=0.7"],
    "karaoke": ["pan=stereo|c0=c0-c1|c1=c1-c0"],   # remove center-panned vocals
    "trebleboost": ["treble=g=10"],
}

# Presets bundle a tempo/pitch pair with an effect filter set.
EFFECT_PRESETS: Dict[str, Dict[str, Any]] = {
    "off":       {"speed": 1.0,  "pitch": 0,  "effect": "off"},
    "nightcore": {"speed": 1.25, "pitch": 3,  "effect": "nightcore"},
    "vaporwave": {"speed": 0.85, "pitch": -3, "effect": "vaporwave"},
    "bassboost": {"speed": 1.0,  "pitch": 0,  "effect": "bassboost"},
    "8d":        {"speed": 1.0,  "pitch": 0,  "effect": "8d"},
    "lofi":      {"speed": 0.9,  "pitch": 0,  "effect": "lofi"},
    "echo":      {"speed": 1.0,  "pitch": 0,  "effect": "echo"},
    "reverb":    {"speed": 1.0,  "pitch": 0,  "effect": "reverb"},
    "tremolo":   {"speed": 1.0,  "pitch": 0,  "effect": "tremolo"},
    "karaoke":   {"speed": 1.0,  "pitch": 0,  "effect": "karaoke"},
    "trebleboost": {"speed": 1.0, "pitch": 0, "effect": "trebleboost"},
}

EFFECT_LABELS: Dict[str, str] = {
    "off": "オフ",
    "nightcore": "ナイトコア",
    "vaporwave": "ベイパーウェイブ",
    "bassboost": "低音ブースト",
    "8d": "8Dオーディオ",
    "lofi": "Lo-Fi",
    "echo": "エコー",
    "reverb": "リバーブ",
    "tremolo": "トレモロ",
    "karaoke": "ボーカルカット",
    "trebleboost": "高音ブースト",
}
