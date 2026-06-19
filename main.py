import os
import asyncio
import random
import logging
from typing import Optional, Dict, List, Any
import yt_dlp
import tempfile
import time
import requests
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands

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

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

if not os.path.isdir(SOUNDS_DIR):
    os.makedirs(SOUNDS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("niconico-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)


class GuildState:
    """Manages per-guild playback state."""
    def __init__(self):
        self.queue: List[Dict[str, Any]] = []
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_song: Optional[Dict[str, Any]] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.is_playing_sound: bool = False
        self.loop_mode: str = "off"  # "off" | "song" | "queue"
        self.skip_flag: bool = False
        self.speed: float = 1.0          # playback tempo (0.5–2.0), pitch preserved
        self.pitch: int = 0              # pitch shift in semitones (-12–+12)
        self.volume: int = 100           # playback volume in percent (0–200)
        self.effect: str = "off"         # active effect preset (see EFFECT_FILTERS)
        self.seek_position: float = 0.0  # start offset (s) of the current FFmpeg source
        self.loops_at_swap: int = 0      # player.loops captured when seek_position was set
        self.speed_at_swap: float = 1.0  # tempo active for the current source segment
        self.resume_position: float = 0.0  # song position to resume at after a sound effect
        self.np_message: Optional[discord.Message] = None  # live now-playing message
        self.np_updater: Optional[asyncio.Task] = None      # progress-bar refresh loop


guild_states: Dict[int, GuildState] = {}

last_cookie_refresh = 0
COOKIE_TTL = int(os.getenv("COOKIE_TTL", "3600"))
cookie_refresh_lock = asyncio.Lock()

# Idle disconnect timeout (seconds) configurable via env
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "180"))

# How often (seconds) to edit the now-playing embed so the progress bar advances.
NP_UPDATE_INTERVAL = 10

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


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


def fmt_duration(seconds: float) -> str:
    """Format seconds as m:ss (or h:mm:ss past an hour)."""
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(value: str) -> Optional[float]:
    """Parse '90', '1:30' or '1:02:03' into seconds. Returns None if invalid."""
    value = value.strip()
    if not value:
        return None
    try:
        if ":" in value:
            parts = [float(p) for p in value.split(":")]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            return None
        return float(value)
    except ValueError:
        return None


def make_progress_bar(elapsed: float, duration: float, length: int = 18) -> str:
    """Render `0:12` ▬▬🔘▬▬ `4:56`. Empty string when duration is unknown."""
    if not duration or duration <= 0:
        return ""
    ratio = max(0.0, min(1.0, elapsed / duration))
    pos = min(length - 1, int(ratio * length))
    bar = "".join("🔘" if i == pos else "▬" for i in range(length))
    return f"`{fmt_duration(elapsed)}` {bar} `{fmt_duration(duration)}`"


def effect_status_line(state: GuildState) -> str:
    """Summarize active speed/pitch/volume/effect; '' when everything default."""
    parts: List[str] = []
    if abs(state.speed - 1.0) > 1e-6:
        parts.append(f"速度 {state.speed:.2f}x")
    if state.pitch != 0:
        parts.append(f"ピッチ {state.pitch:+d}")
    if state.volume != 100:
        parts.append(f"音量 {state.volume}%")
    if state.effect != "off":
        parts.append(f"効果 {EFFECT_LABELS.get(state.effect, state.effect)}")
    return " ・ ".join(parts)


def create_now_playing_embed(song: Dict[str, Any], *, elapsed: Optional[float] = None,
                             state: Optional[GuildState] = None) -> discord.Embed:
    """Create a 'now playing' embed, optionally with a progress bar and effects."""
    embed = discord.Embed(
        title="再生中",
        description=f"**[{song['title']}]({song['url']})**\nリクエスト: {song.get('requester', '不明')}",
        color=0x00ff00,
    )
    duration = song.get("duration") or 0
    bar = make_progress_bar(elapsed, duration) if elapsed is not None else ""
    if bar:
        embed.add_field(name="再生位置", value=bar, inline=False)
    else:
        embed.add_field(name="再生時間", value=fmt_duration(duration), inline=False)
    if state is not None:
        status = effect_status_line(state)
        if status:
            embed.add_field(name="エフェクト", value=status, inline=False)
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    return embed


def create_queued_embed(song: Dict[str, Any], position: int) -> discord.Embed:
    """Create a 'added to queue' embed."""
    embed = discord.Embed(
        title="キューに追加",
        description=f"**[{song['title']}]({song['url']})** をキューに追加しました (#{position})\nリクエスト: {song.get('requester', '不明')}",
        color=0x00ff00,
    )
    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])
    return embed


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


def save_session_cookies(cookies: requests.cookies.RequestsCookieJar) -> None:
    with open(COOKIE_FILE, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for cookie in cookies:
            domain = cookie.domain
            if not domain.startswith("."):
                domain = "." + domain.lstrip(".")
            path = cookie.path
            secure = "TRUE" if cookie.secure else "FALSE"
            expiry = str(int(cookie.expires)) if cookie.expires else "0"
            f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{cookie.name}\t{cookie.value}\n")
    try:
        os.chmod(COOKIE_FILE, 0o600)
    except Exception as e:
        logger.warning(f"Failed to set restrictive permissions on cookie file: {e}")
    logger.info(f"Saved {len(cookies)} session cookies")


def refresh_nico_cookies_sync(force: bool = False) -> bool:
    global last_cookie_refresh
    now = time.time()
    if not force and (now - last_cookie_refresh) < COOKIE_TTL:
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
            logger.info("Using cached cookies (not expired)")
            return True

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
            with open(COOKIE_FILE, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
                for cookie in cookies:
                    domain = cookie.get("domain", ".nicovideo.jp")
                    if not domain.startswith("."):
                        domain = "." + domain.lstrip(".")
                    path = cookie.get("path", "/")
                    secure = "TRUE" if cookie.get("secure", False) else "FALSE"
                    expiry = str(int(cookie["expiry"])) if "expiry" in cookie else "0"
                    f.write(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{cookie['name']}\t{cookie['value']}\n")

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
    if COOKIE_FILE and not os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
            os.chmod(COOKIE_FILE, 0o600)
        except Exception as e:
            logger.warning(f"Failed to create cookie file: {e}")


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

    if COOKIE_FILE:
        ensure_cookie_file()
        if os.path.exists(COOKIE_FILE):
            ydl_opts["cookiefile"] = COOKIE_FILE

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


def cancel_idle_task(guild_id: int) -> None:
    state = get_state(guild_id)
    if state.idle_task:
        state.idle_task.cancel()
        state.idle_task = None


async def schedule_disconnect(guild_id: int) -> None:
    try:
        await asyncio.sleep(IDLE_TIMEOUT)
        state = get_state(guild_id)
        vc = state.voice_client
        if vc and vc.is_connected() and not vc.is_playing():
            logger.info(f"Idle timeout ({IDLE_TIMEOUT}s), disconnecting")
            await vc.disconnect()
            if guild_id in guild_states:
                del guild_states[guild_id]
    except asyncio.CancelledError:
        pass


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


def cancel_np_updater(state: GuildState) -> None:
    """Stop the running now-playing progress-bar refresh loop, if any."""
    if state.np_updater is not None:
        state.np_updater.cancel()
        state.np_updater = None


def start_np_updater(guild_id: int, interval: float = NP_UPDATE_INTERVAL) -> None:
    """Periodically refresh the now-playing message's progress bar."""
    state = get_state(guild_id)
    cancel_np_updater(state)

    async def _updater():
        try:
            while True:
                await asyncio.sleep(interval)
                vc = state.voice_client
                song = state.current_song
                msg = state.np_message
                if not vc or not vc.is_connected() or not song or not msg:
                    break
                if not (vc.is_playing() or vc.is_paused()):
                    break
                elapsed = current_elapsed(vc, state)
                try:
                    embed = create_now_playing_embed(song, elapsed=elapsed, state=state)
                    await msg.edit(embed=embed)
                except Exception as e:
                    logger.warning(f"Failed to update now playing message: {e}")
                    break
        except asyncio.CancelledError:
            pass

    state.np_updater = asyncio.create_task(_updater())


async def refresh_now_playing(guild_id: int) -> None:
    """Re-render the live now-playing embed to reflect new playback settings."""
    state = get_state(guild_id)
    vc = state.voice_client
    msg = state.np_message
    song = state.current_song
    if not vc or not msg or not song:
        return
    playing = vc.is_playing() or vc.is_paused()
    elapsed = current_elapsed(vc, state) if playing else None
    try:
        embed = create_now_playing_embed(song, elapsed=elapsed, state=state)
        await msg.edit(embed=embed)
    except Exception as e:
        logger.warning(f"Failed to refresh now playing message: {e}")


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
        try:
            old_source.cleanup()
        except Exception as e:
            logger.warning(f"Failed to cleanup old audio source: {e}")
    player = getattr(vc, "_player", None)
    state.seek_position = seek
    state.loops_at_swap = getattr(player, "loops", 0) if player else 0
    state.speed_at_swap = state.speed


def reapply_audio_settings(vc: discord.VoiceClient, state: GuildState) -> None:
    """Re-render the current song in place from the current playback position."""
    swap_source_at(vc, state, current_elapsed(vc, state))


class MusicControls(discord.ui.View):
    """Interactive control buttons attached to the now-playing embed."""

    def __init__(self):
        # Persistent view (timeout=None), registered once via bot.add_view in
        # on_ready so the buttons keep working after a restart. Handlers resolve
        # the guild from the interaction, so no per-guild state is stored here.
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ 一時停止しました。", ephemeral=True)
        elif vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ 再開しました。", ephemeral=True)
        else:
            await interaction.response.send_message("再生していません。", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = get_state(interaction.guild.id)
            state.skip_flag = True
            vc.stop()
            await interaction.response.send_message("⏭️ スキップしました。", ephemeral=True)
        else:
            await interaction.response.send_message("再生していません。", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_idle_task(interaction.guild.id)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        state = guild_states.get(interaction.guild.id)
        if state:
            cancel_np_updater(state)
        if interaction.guild.id in guild_states:
            del guild_states[interaction.guild.id]
        await interaction.response.send_message("⏹️ 停止しました。", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        order = {"off": "song", "song": "queue", "queue": "off"}
        state.loop_mode = order.get(state.loop_mode, "off")
        labels = {"off": "オフ", "song": "1曲リピート", "queue": "キュー全体リピート"}
        await interaction.response.send_message(
            f"🔁 リピート: **{labels[state.loop_mode]}**", ephemeral=True
        )

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        if len(state.queue) < 2:
            await interaction.response.send_message("シャッフルする曲が足りません。", ephemeral=True)
            return
        random.shuffle(state.queue)
        await interaction.response.send_message(
            f"🔀 キュー（{len(state.queue)}曲）をシャッフルしました。", ephemeral=True
        )

    async def _apply_speed_pitch(self, interaction: discord.Interaction, *,
                                 speed: Optional[float] = None,
                                 pitch: Optional[int] = None,
                                 effect: Optional[str] = None):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生していません。", ephemeral=True)
            return
        if state.is_playing_sound:
            await interaction.response.send_message("効果音の再生中は変更できません。", ephemeral=True)
            return
        if speed is not None:
            state.speed = speed
        if pitch is not None:
            state.pitch = pitch
        if effect is not None:
            state.effect = effect
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(
            f"🎚️ 速度 **{state.speed:.2f}x** / "
            f"ピッチ **{state.pitch:+d}半音** に変更",
            ephemeral=True,
        )

    @discord.ui.button(emoji="🐢", label="遅く", style=discord.ButtonStyle.secondary, row=1, custom_id="music:slow_down")
    async def slow_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await self._apply_speed_pitch(
            interaction, speed=round(max(SPEED_MIN, state.speed - SPEED_STEP), 2)
        )

    @discord.ui.button(emoji="🐇", label="速く", style=discord.ButtonStyle.secondary, row=1, custom_id="music:speed_up")
    async def speed_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await self._apply_speed_pitch(
            interaction, speed=round(min(SPEED_MAX, state.speed + SPEED_STEP), 2)
        )

    @discord.ui.button(emoji="🔽", label="ピッチ-", style=discord.ButtonStyle.secondary, row=1, custom_id="music:pitch_down")
    async def pitch_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await self._apply_speed_pitch(interaction, pitch=max(PITCH_MIN, state.pitch - 1))

    @discord.ui.button(emoji="🔼", label="ピッチ+", style=discord.ButtonStyle.secondary, row=1, custom_id="music:pitch_up")
    async def pitch_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        await self._apply_speed_pitch(interaction, pitch=min(PITCH_MAX, state.pitch + 1))

    @discord.ui.button(emoji="🎚️", label="リセット", style=discord.ButtonStyle.primary, row=1, custom_id="music:reset")
    async def reset_effects(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_speed_pitch(interaction, speed=1.0, pitch=0, effect="off")

    async def _apply_preset(self, interaction: discord.Interaction, preset_key: str):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生していません。", ephemeral=True)
            return
        if state.is_playing_sound:
            await interaction.response.send_message("効果音の再生中は変更できません。", ephemeral=True)
            return
        preset = EFFECT_PRESETS[preset_key]
        state.speed = preset["speed"]
        state.pitch = preset["pitch"]
        state.effect = preset["effect"]
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(
            f"🎛️ プリセット **{EFFECT_LABELS.get(preset['effect'], preset_key)}** を適用しました。",
            ephemeral=True,
        )

    @discord.ui.select(
        placeholder="🎛️ エフェクトプリセットを選択…",
        row=2,
        min_values=1,
        max_values=1,
        custom_id="music:preset_select",
        options=[
            discord.SelectOption(label="オフ（通常）", value="off", emoji="🎚️"),
            discord.SelectOption(label="ナイトコア", value="nightcore", emoji="⚡", description="1.25x / +3半音"),
            discord.SelectOption(label="ベイパーウェイブ", value="vaporwave", emoji="🌊", description="0.85x / -3半音"),
            discord.SelectOption(label="低音ブースト", value="bassboost", emoji="🔊"),
            discord.SelectOption(label="8Dオーディオ", value="8d", emoji="🎧"),
            discord.SelectOption(label="Lo-Fi", value="lofi", emoji="📼", description="0.9x"),
            discord.SelectOption(label="エコー", value="echo", emoji="📢"),
            discord.SelectOption(label="リバーブ", value="reverb", emoji="🏛️"),
            discord.SelectOption(label="トレモロ", value="tremolo", emoji="📳"),
            discord.SelectOption(label="ボーカルカット", value="karaoke", emoji="🎤"),
            discord.SelectOption(label="高音ブースト", value="trebleboost", emoji="🔔"),
        ],
    )
    async def preset_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._apply_preset(interaction, select.values[0])


async def advance_queue(guild_id: int, finished_song: Dict[str, Any]) -> None:
    """Decide what to enqueue next based on loop/skip state, then play."""
    state = get_state(guild_id)
    if state.skip_flag:
        # Manual skip overrides loop: drop the finished song and move on.
        state.skip_flag = False
    elif state.loop_mode == "song":
        finished_song["local_file"] = None  # temp file already cleaned up
        state.queue.insert(0, finished_song)
    elif state.loop_mode == "queue":
        finished_song["local_file"] = None
        state.queue.append(finished_song)
    await play_next(guild_id)


async def play_next(guild_id: int, announce: bool = True) -> None:
    state = get_state(guild_id)
    vc = state.voice_client

    if not vc or not vc.is_connected():
        return

    if len(state.queue) == 0:
        state.current_song = None
        cancel_np_updater(state)
        state.np_message = None
        logger.info(f"Queue empty, scheduling disconnect in {IDLE_TIMEOUT}s")
        cancel_idle_task(guild_id)
        state.idle_task = asyncio.create_task(schedule_disconnect(guild_id))
        return

    cancel_idle_task(guild_id)

    song = state.queue.pop(0)
    state.current_song = song
    logger.info(f"Playing: {song['title']}")

    if announce:
        try:
            embed = create_now_playing_embed(song, elapsed=0.0, state=state)
            channel_id = song.get("text_channel_id")
            guild = vc.channel.guild
            text_channel = guild.get_channel(channel_id) if channel_id else None
            if not text_channel:
                # Fall back to the first channel the bot may actually post in.
                text_channel = next(
                    (ch for ch in guild.text_channels
                     if ch.permissions_for(guild.me).send_messages),
                    None,
                )
            if text_channel:
                state.np_message = await text_channel.send(embed=embed, view=MusicControls())
                start_np_updater(guild_id)
        except Exception as e:
            logger.warning(f"Failed to send now playing message: {e}")

    def after_play(error):
        if error:
            logger.error(f"Play error: {error}")
        local_file = song.get("local_file")
        if local_file and os.path.exists(local_file):
            try:
                os.remove(local_file)
                song["local_file"] = None
                logger.info(f"Cleaned up temp file: {local_file}")
            except Exception as e:
                logger.warning(f"Failed to remove temp file: {e}")
        if not state.is_playing_sound:
            asyncio.run_coroutine_threadsafe(advance_queue(guild_id, song), bot.loop)

    try:
        if song.get("needs_local") and not song.get("local_file"):
            local_file = await asyncio.get_running_loop().run_in_executor(
                None, download_audio, song["url"]
            )
            if not local_file:
                logger.error("Failed to download audio")
                await play_next(guild_id)
                return
            song["local_file"] = local_file

        # A fresh FFmpeg process starts at loops=0; reset the seek bookkeeping.
        state.seek_position = 0.0
        state.loops_at_swap = 0
        state.speed_at_swap = state.speed
        source = make_audio_source(song, state, seek=0.0)
        vc.play(source, after=after_play)
    except Exception as e:
        logger.error(f"Play failed: {e}")
        await play_next(guild_id)


def download_audio(url: str) -> str:
    """Download audio to a temp file and return the path.

    Used for niconico and YouTube; for YouTube the request is routed through
    the residential proxy (via build_ydl_opts) so the IP-locked media URL is
    fetched from the same IP that extracted it.
    """
    tmpdir = tempfile.gettempdir()
    output_template = os.path.join(tmpdir, "dl_%(id)s.%(ext)s")

    ydl_opts = build_ydl_opts(url, outtmpl=output_template)

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


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Sync error: {e}")
    # on_ready can fire again on gateway reconnects; run startup work only once.
    if not getattr(bot, "_startup_done", False):
        bot._startup_done = True
        # Register the persistent control view so now-playing buttons keep
        # working after a restart, and start the single cookie-refresh loop.
        bot.add_view(MusicControls())
        bot.loop.create_task(background_cookie_refresh())


async def background_cookie_refresh():
    await asyncio.sleep(2)
    while True:
        try:
            async with cookie_refresh_lock:
                await asyncio.get_running_loop().run_in_executor(None, refresh_nico_cookies_sync, True)
        except Exception as e:
            logger.error(f"Background cookie refresh error: {e}")
        await asyncio.sleep(COOKIE_TTL)


@bot.event
async def on_message(message):
    if message.author.bot:
        await bot.process_commands(message)
        return

    if not message.guild:
        await bot.process_commands(message)
        return

    guild_id = message.guild.id
    content = message.content

    if content in ["んあー", "んあーと"]:
        state = get_state(guild_id)
        if state.is_playing_sound:
            await bot.process_commands(message)
            return

        vc = state.voice_client
        if not vc or not vc.is_connected():
            await bot.process_commands(message)
            return

        if not vc.is_playing():
            await bot.process_commands(message)
            return

        mp3_file = os.path.join(SOUNDS_DIR, "na-.mp3")
        if not os.path.exists(mp3_file):
            logger.warning(f"Sound file not found: {mp3_file}")
            await bot.process_commands(message)
            return

        logger.info(f"Playing sound effect for trigger: {content}")
        state.resume_position = current_elapsed(vc, state)  # resume here after the effect
        state.is_playing_sound = True
        vc.stop()

        def after_sound(error):
            if error:
                logger.error(f"Sound effect error: {error}")
            try:
                asyncio.run_coroutine_threadsafe(
                    restart_song(guild_id), bot.loop
                )
            except Exception as e:
                logger.error(f"Failed to schedule restart: {e}")
                state.is_playing_sound = False

        try:
            source = discord.FFmpegOpusAudio(
                mp3_file,
                options="-c:a libopus -b:a 192k -ar 48000 -ac 2",
            )
            vc.play(source, after=after_sound)
        except Exception as e:
            logger.error(f"Failed to play sound: {e}")
            state.is_playing_sound = False

    await bot.process_commands(message)


async def restart_song(guild_id: int) -> None:
    await asyncio.sleep(0.3)
    state = get_state(guild_id)
    vc = state.voice_client

    song = state.current_song
    if not song:
        logger.warning("No current song found")
        state.is_playing_sound = False
        return

    if not vc or not vc.is_connected():
        logger.warning(f"VC not available for guild {guild_id}")
        state.is_playing_sound = False
        return

    def finish(error=None):
        # Shared teardown for both the skip path and the normal end-of-song path.
        if error:
            logger.error(f"Restart error: {error}")
        state.is_playing_sound = False
        local_file = song.get("local_file")
        if local_file and os.path.exists(local_file):
            try:
                os.remove(local_file)
                song["local_file"] = None
                logger.info(f"Cleaned up temp file: {local_file}")
            except Exception as e:
                logger.warning(f"Failed to remove temp file: {e}")
        # The song is done (finished or skipped) — advance the queue.
        asyncio.run_coroutine_threadsafe(advance_queue(guild_id, song), bot.loop)

    # A skip requested during the sound effect should move on, not replay the song.
    if state.skip_flag:
        logger.info("Skip requested during sound effect; advancing instead of restarting")
        finish()
        return

    seek = max(0.0, state.resume_position)
    logger.info(f"Resuming song at {fmt_duration(seek)}: {song['title']}")

    try:
        if song.get("needs_local") and not song.get("local_file"):
            local_file = await asyncio.get_running_loop().run_in_executor(
                None, download_audio, song["url"]
            )
            if not local_file:
                logger.error("Failed to download audio for restart")
                state.is_playing_sound = False
                return
            song["local_file"] = local_file

        # Resume from where the sound effect interrupted, keeping speed/pitch.
        state.seek_position = seek
        state.loops_at_swap = 0
        state.speed_at_swap = state.speed
        source = make_audio_source(song, state, seek=seek)
        vc.play(source, after=finish)
    except Exception as e:
        logger.error(f"Failed to restart song: {e}")
        state.is_playing_sound = False


@bot.tree.command(name="play", description="Play a song from NicoNico or YouTube")
@app_commands.describe(query="NicoNico URL, YouTube URL, or search keyword")
async def play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice:
        await interaction.response.send_message("VCに参加してください。")
        return

    await interaction.response.defer()

    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client

    loop = asyncio.get_running_loop()
    try:
        song = await asyncio.wait_for(
            loop.run_in_executor(None, extract_audio_url, query),
            timeout=60
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("曲の取得がタイムアウトしました。")
        return
    except Exception as e:
        await interaction.followup.send(f"曲が見つかりません: {str(e)}")
        return

    song["text_channel_id"] = interaction.channel.id
    song["requester"] = interaction.user.display_name

    if not vc:
        try:
            vc = await channel.connect(timeout=15)
            state = get_state(interaction.guild.id)
            state.voice_client = vc
        except Exception as e:
            await interaction.followup.send(f"VC接続失敗: {str(e)}")
            return
    elif vc.channel != channel:
        try:
            await vc.move_to(channel)
        except Exception as e:
            await interaction.followup.send(f"チャンネル移動失敗: {str(e)}")
            return

    cancel_idle_task(interaction.guild.id)

    state = get_state(interaction.guild.id)
    state.queue.append(song)

    if vc.is_playing() or vc.is_paused():
        embed = create_queued_embed(song, len(state.queue))
        await interaction.followup.send(embed=embed)
    else:
        state.current_song = song
        embed = create_now_playing_embed(song, elapsed=0.0, state=state)
        state.np_message = await interaction.followup.send(
            embed=embed, view=MusicControls()
        )
        start_np_updater(interaction.guild.id)
        # /play already announced this song, so don't re-announce in play_next.
        await play_next(interaction.guild.id, announce=False)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("再生していません。", ephemeral=True)
        return
    state = get_state(interaction.guild.id)
    state.skip_flag = True
    vc.stop()
    await interaction.response.send_message("スキップしました！")


@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.queue:
        await interaction.response.send_message("キューは空です。")
        return
    desc = "\n".join(f"{i+1}. **[{s['title']}]({s['url']})** (by {s.get('requester', '不明')})" for i, s in enumerate(state.queue[:10]))
    embed = discord.Embed(title="キュー", description=desc, color=0x00ff00)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="loop", description="Set repeat mode (off / song / queue)")
@app_commands.describe(mode="リピートモード")
@app_commands.choices(mode=[
    app_commands.Choice(name="オフ", value="off"),
    app_commands.Choice(name="1曲リピート", value="song"),
    app_commands.Choice(name="キュー全体リピート", value="queue"),
])
async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    state = get_state(interaction.guild.id)
    state.loop_mode = mode.value
    labels = {"off": "オフ", "song": "1曲リピート", "queue": "キュー全体リピート"}
    await interaction.response.send_message(f"🔁 リピート: **{labels[state.loop_mode]}**")


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if len(state.queue) < 2:
        await interaction.response.send_message("シャッフルする曲が足りません。", ephemeral=True)
        return
    random.shuffle(state.queue)
    await interaction.response.send_message(f"🔀 キュー（{len(state.queue)}曲）をシャッフルしました。")


@bot.tree.command(name="speed", description="Set playback speed (0.5-2.0x, pitch preserved)")
@app_commands.describe(rate="再生速度 (0.5〜2.0)")
async def speed_cmd(interaction: discord.Interaction, rate: app_commands.Range[float, 0.5, 2.0]):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    state.speed = round(rate, 2)
    if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
    await interaction.response.send_message(f"🎚️ 速度を **{state.speed:.2f}x** にしました。")


@bot.tree.command(name="pitch", description="Set pitch shift in semitones (-12 to +12)")
@app_commands.describe(semitones="ピッチ (-12〜+12半音)")
async def pitch_cmd(interaction: discord.Interaction, semitones: app_commands.Range[int, -12, 12]):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    state.pitch = semitones
    if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
    await interaction.response.send_message(f"🎚️ ピッチを **{state.pitch:+d}半音** にしました。")


@bot.tree.command(name="seek", description="Jump to a position in the current song")
@app_commands.describe(position="再生位置（秒 または mm:ss、例: 90 / 1:30）")
async def seek_cmd(interaction: discord.Interaction, position: str):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("再生していません。", ephemeral=True)
        return
    if state.is_playing_sound:
        await interaction.response.send_message("効果音の再生中は変更できません。", ephemeral=True)
        return
    secs = parse_time(position)
    if secs is None or secs < 0:
        await interaction.response.send_message(
            "時間の形式が不正です（例: `90` または `1:30`）。", ephemeral=True
        )
        return
    duration = (state.current_song or {}).get("duration") or 0
    if duration and secs >= duration:
        await interaction.response.send_message(
            f"曲の長さ（{fmt_duration(duration)}）以内で指定してください。", ephemeral=True
        )
        return
    swap_source_at(vc, state, secs)
    await interaction.response.send_message(f"⏩ **{fmt_duration(secs)}** へシークしました。")


@bot.tree.command(name="volume", description="Set playback volume (0-200%)")
@app_commands.describe(level="音量 (0〜200)")
async def volume_cmd(interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    state.volume = level
    if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
    await interaction.response.send_message(f"🔊 音量を **{level}%** にしました。")


@bot.tree.command(name="preset", description="Apply an audio effect preset")
@app_commands.describe(name="エフェクトプリセット")
@app_commands.choices(name=[
    app_commands.Choice(name="オフ（通常）", value="off"),
    app_commands.Choice(name="ナイトコア", value="nightcore"),
    app_commands.Choice(name="ベイパーウェイブ", value="vaporwave"),
    app_commands.Choice(name="低音ブースト", value="bassboost"),
    app_commands.Choice(name="8Dオーディオ", value="8d"),
    app_commands.Choice(name="Lo-Fi", value="lofi"),
    app_commands.Choice(name="エコー", value="echo"),
    app_commands.Choice(name="リバーブ", value="reverb"),
    app_commands.Choice(name="トレモロ", value="tremolo"),
    app_commands.Choice(name="ボーカルカット", value="karaoke"),
    app_commands.Choice(name="高音ブースト", value="trebleboost"),
])
async def preset_cmd(interaction: discord.Interaction, name: app_commands.Choice[str]):
    state = get_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    preset = EFFECT_PRESETS[name.value]
    state.speed = preset["speed"]
    state.pitch = preset["pitch"]
    state.effect = preset["effect"]
    if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
        reapply_audio_settings(vc, state)
        await refresh_now_playing(interaction.guild.id)
    await interaction.response.send_message(f"🎛️ プリセット **{name.name}** を適用しました。")


@bot.tree.command(name="remove", description="Remove a song from the queue by position")
@app_commands.describe(position="削除するキューの番号（1から）")
async def remove_cmd(interaction: discord.Interaction, position: int):
    state = get_state(interaction.guild.id)
    if not state.queue:
        await interaction.response.send_message("キューは空です。", ephemeral=True)
        return
    if position < 1 or position > len(state.queue):
        await interaction.response.send_message(
            f"1〜{len(state.queue)} の範囲で指定してください。", ephemeral=True
        )
        return
    removed = state.queue.pop(position - 1)
    await interaction.response.send_message(f"🗑️ 削除しました: **{removed['title']}**")


@bot.tree.command(name="clear", description="Clear the queue without disconnecting")
async def clear_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    count = len(state.queue)
    state.queue.clear()
    await interaction.response.send_message(
        f"🧹 キューをクリアしました（{count}曲）。再生中の曲は継続します。"
    )


@bot.tree.command(name="join", description="Join your voice channel")
async def join_cmd(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("先にVCに参加してください。", ephemeral=True)
        return
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    state = get_state(interaction.guild.id)
    try:
        if vc:
            await vc.move_to(channel)
        else:
            vc = await channel.connect(timeout=15)
            state.voice_client = vc
    except Exception as e:
        await interaction.response.send_message(f"VC接続失敗: {str(e)}", ephemeral=True)
        return
    await interaction.response.send_message(f"🔊 接続しました: **{channel.name}**")


@bot.tree.command(name="leave", description="Disconnect from the voice channel")
async def leave_cmd(interaction: discord.Interaction):
    cancel_idle_task(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        await interaction.response.send_message("VCに接続していません。", ephemeral=True)
        return
    vc.stop()
    await vc.disconnect()
    state = guild_states.get(interaction.guild.id)
    if state:
        cancel_np_updater(state)
    if interaction.guild.id in guild_states:
        del guild_states[interaction.guild.id]
    await interaction.response.send_message("👋 退出しました。")


@bot.tree.command(name="help", description="Show available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="INMERMUSIC BOT コマンド一覧", color=0x00ff00)
    embed.add_field(name="/play <URL/キーワード>", value="ニコニコ/YouTube/検索キーワードで再生", inline=False)
    embed.add_field(name="/skip", value="現在の曲をスキップ", inline=True)
    embed.add_field(name="/pause・/resume", value="一時停止・再開", inline=True)
    embed.add_field(name="/stop", value="停止してキュー削除・退出", inline=True)
    embed.add_field(name="/queue", value="キューを表示", inline=True)
    embed.add_field(name="/nowplaying", value="再生中の曲を表示", inline=True)
    embed.add_field(name="/loop <mode>", value="リピート (off/song/queue)", inline=True)
    embed.add_field(name="/shuffle", value="キューをシャッフル", inline=True)
    embed.add_field(name="/speed <0.5-2.0>", value="再生速度（ピッチ維持）", inline=True)
    embed.add_field(name="/pitch <-12〜12>", value="ピッチ（半音単位）", inline=True)
    embed.add_field(name="/volume <0-200>", value="音量調整（%）", inline=True)
    embed.add_field(name="/seek <時間>", value="再生位置へジャンプ (例 1:30)", inline=True)
    embed.add_field(name="/preset <名前>", value="エフェクト（ナイトコア等）", inline=True)
    embed.add_field(name="/remove <番号>", value="キューから曲を削除", inline=True)
    embed.add_field(name="/clear", value="キューをクリア（再生は継続）", inline=True)
    embed.add_field(name="/join・/leave", value="VCに参加・退出", inline=True)
    embed.add_field(name="/na-", value="効果音（同一曲中1回）", inline=True)
    embed.add_field(name="/refresh", value="ニコニコCookie更新", inline=True)
    embed.add_field(name="再生中ボタン", value="🐢🐇 速度 / 🔽🔼 ピッチ / 🎚️ リセット", inline=False)
    embed.add_field(name="メッセージトリガー", value="`んあー` / `んあーと` で効果音", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stop", description="Stop playing and clear the queue")
async def stop(interaction: discord.Interaction):
    cancel_idle_task(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    state = guild_states.get(interaction.guild.id)
    if state:
        cancel_np_updater(state)
    if interaction.guild.id in guild_states:
        del guild_states[interaction.guild.id]
    await interaction.response.send_message("停止してキューをクリアしました。")


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("一時停止しました。")
    else:
        await interaction.response.send_message("再生していません。", ephemeral=True)


@bot.tree.command(name="resume", description="Resume the paused song")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("再開しました。")
    else:
        await interaction.response.send_message("一時停止していません。")


@bot.tree.command(name="nowplaying", description="Show current playing song")
async def nowplaying(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state.current_song:
        await interaction.response.send_message("再生中の曲はありません。")
        return
    vc = interaction.guild.voice_client
    elapsed = current_elapsed(vc, state) if vc and (vc.is_playing() or vc.is_paused()) else None
    embed = create_now_playing_embed(state.current_song, elapsed=elapsed, state=state)
    await interaction.response.send_message(embed=embed, view=MusicControls())


@bot.tree.command(name="na-", description="ンアッー!(≧д≦)")
async def na_command(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    state = get_state(guild_id)

    vc = state.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("VCに接続していません。", ephemeral=True)
        return

    if not vc.is_playing():
        await interaction.response.send_message("再生していません。", ephemeral=True)
        return

    mp3_file = os.path.join(SOUNDS_DIR, "na-.mp3")
    if not os.path.exists(mp3_file):
        await interaction.response.send_message("効果音ファイルが見つかりません。", ephemeral=True)
        return

    if state.is_playing_sound:
        await interaction.response.send_message("同一楽曲再生中に1度しか流せません")
        return

    logger.info("Playing sound effect via /na-")
    state.resume_position = current_elapsed(vc, state)  # resume here after the effect
    state.is_playing_sound = True
    if vc and vc.is_connected():
        vc.stop()

    def after_sound(error):
        if error:
            logger.error(f"Sound effect error: {error}")
        try:
            asyncio.run_coroutine_threadsafe(restart_song(guild_id), bot.loop)
        except Exception as e:
            logger.error(f"Failed to schedule restart: {e}")
            state.is_playing_sound = False

    try:
        source = discord.FFmpegOpusAudio(
            mp3_file,
            options="-c:a libopus -b:a 192k -ar 48000 -ac 2",
        )
        vc.play(source, after=after_sound)
        await interaction.response.send_message("ンアッー!", ephemeral=True)
    except Exception as e:
        logger.error(f"Failed to play sound: {e}")
        state.is_playing_sound = False
        if vc and vc.is_connected():
            vc.resume()
        await interaction.response.send_message("効果音の再生に失敗しました。", ephemeral=True)


@bot.tree.command(name="refresh", description="Refresh niconico cookies")
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    global last_cookie_refresh
    last_cookie_refresh = 0
    success = await asyncio.get_running_loop().run_in_executor(None, refresh_nico_cookies_sync, True)
    if success:
        await interaction.followup.send("Cookieを更新しました！", ephemeral=True)
    else:
        await interaction.followup.send("Cookieの更新に失敗しました", ephemeral=True)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild = member.guild
    state = guild_states.get(guild.id)
    if not state or not state.voice_client or not state.voice_client.is_connected():
        return

    bot_channel = state.voice_client.channel
    human_members = [m for m in bot_channel.members if not m.bot]
    if len(human_members) == 0:
        logger.info("All users left the voice channel, disconnecting bot")
        cancel_idle_task(guild.id)
        text_channel = None
        if state.current_song and state.current_song.get("text_channel_id"):
            text_channel = guild.get_channel(state.current_song["text_channel_id"])
        if not text_channel:
            text_channel = next((ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), None)
        cancel_np_updater(state)
        await state.voice_client.disconnect()
        if guild.id in guild_states:
            del guild_states[guild.id]
        if text_channel:
            await text_channel.send("誰も居なくなったので退出しました。")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Error: Please set DISCORD_TOKEN in .env file")
        exit(1)
    bot.run(TOKEN)
