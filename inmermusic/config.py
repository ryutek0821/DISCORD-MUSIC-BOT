"""Environment configuration, logging, and audio/effect constants.

Lowest layer: imports nothing from the package. COOKIE_FILE may be monkeypatched
in tests, so other modules read it as ``config.COOKIE_FILE`` (attribute access)
rather than importing the value by name.
"""
import os
import logging
import tempfile
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")


def _parse_id_list(raw: Optional[str]) -> frozenset:
    """Parse a comma-separated Discord ID list, ignoring junk entries."""
    if not raw:
        return frozenset()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.getLogger("niconico-bot").warning(
                f"Ignoring non-numeric BOT_ADMIN_IDS entry: {part!r}")
    return frozenset(ids)


# Discord user IDs allowed to run the operator commands (/nicosession, /settings).
# Server "Manage Server" permission answers "is this person a moderator of the
# guild", not "does this person operate the bot" — the two came apart in
# practice, locking the operator out of their own bot. When this is empty the
# manage_guild check still applies, so an unconfigured deployment is neither
# wide open nor locked out.
BOT_ADMIN_IDS = _parse_id_list(os.getenv("BOT_ADMIN_IDS"))
COOKIE_FILE = os.getenv("COOKIE_FILE")
NICO_EMAIL = os.getenv("NICO_EMAIL")
NICO_PASSWORD = os.getenv("NICO_PASSWORD")
# A niconico `user_session` cookie value, obtained by hand from a logged-in
# browser. niconico moved its login to an MFA-capable SPA, so there is no
# scriptable credential login left to automate; a pasted session is the
# supported way to authenticate. These sessions are long-lived (months).
NICO_SESSION = os.getenv("NICO_SESSION")
# Route YouTube traffic through a residential-IP proxy (e.g. a Tailscale RPi)
# so YouTube's datacenter-IP bot detection doesn't block extraction. The
# googlevideo media URLs are IP-locked to the extractor, so downloads must use
# the same proxy; that's why YouTube is fetched to a local file like niconico.
YT_PROXY = os.getenv("YT_PROXY")
# Comma-separated failover list. Keep YT_PROXY as a backward-compatible
# single-proxy fallback.
YT_PROXIES = [
    value.strip() for value in os.getenv("YT_PROXIES", "").split(",")
    if value.strip()
]
if not YT_PROXIES and YT_PROXY:
    YT_PROXIES = [YT_PROXY]

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

# Guild credentials must survive rsync --delete deployments, so keep them
# outside the repository in the user's XDG data directory.
_DEFAULT_STATE_DIR = os.path.abspath(
    os.path.expanduser("~/.local/share/inmermusic"))
STATE_DIR = os.path.abspath(os.path.expanduser(
    os.getenv("STATE_DIR", _DEFAULT_STATE_DIR)))
try:
    repo_path = os.path.realpath(_REPO_ROOT)
    if os.path.commonpath([repo_path, os.path.realpath(STATE_DIR)]) == repo_path:
        logger.warning(f"State directory {STATE_DIR} is inside the repository; "
                       f"using {_DEFAULT_STATE_DIR} instead")
        STATE_DIR = _DEFAULT_STATE_DIR
except ValueError:
    pass
try:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
except OSError as e:
    logger.warning(f"State directory {STATE_DIR} is unavailable; "
                   f"guild sessions will not be persisted: {e}")

# Store downloaded audio on real disk by default instead of RAM-backed /tmp.
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/var/tmp/inmermusic")
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    with tempfile.TemporaryFile(dir=DOWNLOAD_DIR):
        pass
except OSError as e:
    fallback_dir = tempfile.gettempdir()
    logger.warning(f"Download directory {DOWNLOAD_DIR} is unavailable; "
                   f"falling back to {fallback_dir}: {e}")
    DOWNLOAD_DIR = fallback_dir

# Idle disconnect timeout (seconds) configurable via env
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "180"))
# A persisted queue survives an unintentional disconnect (empty VC, an admin
# kicking the bot, an idle timeout) but not forever: past this age it is
# treated as abandoned rather than restored on the next /join.
QUEUE_RESTORE_TTL = int(os.getenv("QUEUE_RESTORE_TTL", str(24 * 3600)))

# `play_counts` only started accumulating when the stats tables shipped
# (c3585da, 2026-08-04 11:03 JST); every play before this instant is missing
# from the ranking. This is the *commit* time, not the deploy time — the few
# minutes between them still ran the old code, so anything in that window went
# uncounted. Erring early only under-counts those minutes; erring late would
# double-count real plays. /statsbackfill starts from here and walks backwards,
# lowering the per-guild floor as it goes.
PLAY_COUNTS_EPOCH = int(os.getenv("PLAY_COUNTS_EPOCH", "1785808984"))
# Upper bound on the messages /statsbackfill reads from one channel, so a
# multi-year channel can't hold the interaction (and the API) hostage.
BACKFILL_SCAN_LIMIT = int(os.getenv("BACKFILL_SCAN_LIMIT", "5000"))

# Max seconds to wait for a single audio download before giving up, so a stalled
# fetch can't wedge the queue. Also passed to yt-dlp as socket_timeout (capped).
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "120"))
MAX_TRACK_DURATION = int(os.getenv("MAX_TRACK_DURATION", "1800"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "100"))
MAX_PLAYLIST_NAME_LEN = 50
MAX_PLAYLISTS_PER_GUILD = int(os.getenv("MAX_PLAYLISTS_PER_GUILD", "25"))
MAX_PLAYLIST_SIZE = int(os.getenv("MAX_PLAYLIST_SIZE", "50"))
# How many favorites /favorite list shows and /favorite play / remove address by
# number. Display and removal must share this window, or the position a user
# reads and the position that gets deleted can refer to different tracks.
FAVORITES_PAGE_SIZE = 25
# Prefetch is deliberately bounded to one queued track per guild. Reject an
# unexpectedly large file instead of allowing a single guild to fill the disk.
PREFETCH_MAX_BYTES = int(os.getenv("PREFETCH_MAX_BYTES", str(256 * 1024 * 1024)))
# Hard ceiling handed to yt-dlp, so an oversized track is refused *before* the
# transfer instead of being downloaded in full and then rejected. Playlist
# entries arrive without a duration, so this is the only bound that always
# applies. Keep it >= PREFETCH_MAX_BYTES or prefetch would reject what the
# main download path accepts.
MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(256 * 1024 * 1024)))

# How often (seconds) to reap orphaned dl_* temp dirs. A startup-only sweep
# leaves anything leaked mid-run sitting on disk until the next restart.
TEMP_SWEEP_INTERVAL = int(os.getenv("TEMP_SWEEP_INTERVAL", "3600"))

# Autoplay (radio mode): how many tracks to append per refill, and how many
# autoplay tracks may play back-to-back before the bot stops refilling. The
# streak cap is what keeps an empty-but-occupied VC from playing forever; any
# user-queued song resets it.
AUTOPLAY_BATCH = int(os.getenv("AUTOPLAY_BATCH", "3"))
AUTOPLAY_MAX_STREAK = int(os.getenv("AUTOPLAY_MAX_STREAK", "50"))

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

# EBU R128 loudness normalization targets for the optional `loudnorm` filter.
# -14 LUFS / -1 dBTP matches what streaming services normalize to, so a queue
# mixing NicoNico and YouTube lands at a consistent perceived level.
LOUDNORM_TARGET_I = -14.0    # integrated loudness (LUFS)
LOUDNORM_TARGET_TP = -1.0    # true peak ceiling (dBTP)
LOUDNORM_TARGET_LRA = 11.0   # loudness range

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
    "chipmunk": [],                        # pitch only (set via preset)
    "deep": [],                            # pitch only (set via preset)
    "chorus": ["chorus=0.7:0.9:55:0.4:0.25:2"],
    "phaser": ["aphaser"],
    "flanger": ["flanger"],
    "vibrato": ["vibrato=f=7:d=0.9"],
    "telephone": ["highpass=f=300", "lowpass=f=3400"],
    "crystalizer": ["crystalizer=i=2.5"],
    "wide": ["extrastereo=m=2.5"],
    "underwater": ["lowpass=f=500"],
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
    "chipmunk":  {"speed": 1.0,  "pitch": 7,  "effect": "chipmunk"},
    "deep":      {"speed": 1.0,  "pitch": -7, "effect": "deep"},
    "chorus":    {"speed": 1.0,  "pitch": 0,  "effect": "chorus"},
    "phaser":    {"speed": 1.0,  "pitch": 0,  "effect": "phaser"},
    "flanger":   {"speed": 1.0,  "pitch": 0,  "effect": "flanger"},
    "vibrato":   {"speed": 1.0,  "pitch": 0,  "effect": "vibrato"},
    "telephone": {"speed": 1.0,  "pitch": 0,  "effect": "telephone"},
    "crystalizer": {"speed": 1.0, "pitch": 0, "effect": "crystalizer"},
    "wide":      {"speed": 1.0,  "pitch": 0,  "effect": "wide"},
    "underwater": {"speed": 1.0, "pitch": 0,  "effect": "underwater"},
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
    "chipmunk": "チップマンク",
    "deep": "重低音ボイス",
    "chorus": "コーラス",
    "phaser": "フェイザー",
    "flanger": "フランジャー",
    "vibrato": "ビブラート",
    "telephone": "電話越し",
    "crystalizer": "クリスタル",
    "wide": "ワイドステレオ",
    "underwater": "水中",
}

# Emoji per preset, used by the now-playing dropdown. Kept here so the UI and
# slash choices can be generated from config (one source of truth). Must cover
# every EFFECT_PRESETS key (enforced by tests).
EFFECT_EMOJI: Dict[str, str] = {
    "off": "🎚️", "nightcore": "⚡", "vaporwave": "🌊", "bassboost": "🔊",
    "8d": "🎧", "lofi": "📼", "echo": "📢", "reverb": "🏛️", "tremolo": "📳",
    "karaoke": "🎤", "trebleboost": "🔔", "chipmunk": "🐿️", "deep": "😈",
    "chorus": "👥", "phaser": "🌀", "flanger": "✈️", "vibrato": "〰️",
    "telephone": "☎️", "crystalizer": "💎", "wide": "↔️", "underwater": "🫧",
}


def list_sound_names() -> List[str]:
    """Sorted names (without .mp3) of the soundboard files in SOUNDS_DIR."""
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(SOUNDS_DIR)
                      if f.lower().endswith(".mp3"))
    except OSError:
        return []


def resolve_sound(name: str) -> Optional[str]:
    """Map a soundboard name to its file path, guarding against path traversal."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = os.path.join(SOUNDS_DIR, name + ".mp3")
    return path if os.path.isfile(path) else None
