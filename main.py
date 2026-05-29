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


guild_states: Dict[int, GuildState] = {}

last_cookie_refresh = 0
COOKIE_TTL = int(os.getenv("COOKIE_TTL", "3600"))
cookie_refresh_lock = asyncio.Lock()

# Idle disconnect timeout (seconds) configurable via env
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "180"))


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


def create_now_playing_embed(song: Dict[str, Any]) -> discord.Embed:
    """Create a 'now playing' embed."""
    duration_str = f"{song['duration'] // 60}:{song['duration'] % 60:02d}"
    embed = discord.Embed(
        title="再生中",
        description=f"**[{song['title']}]({song['url']})**\n再生時間: {duration_str}\nリクエスト: {song.get('requester', '不明')}",
        color=0x00ff00,
    )
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

            return {
                "url": url,
                "audio_url": audio_url,
                "title": info.get("title", "Unknown"),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", ""),
                "is_niconico": is_niconico,
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


class MusicControls(discord.ui.View):
    """Interactive control buttons attached to the now-playing embed."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary)
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

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = get_state(interaction.guild.id)
            state.skip_flag = True
            vc.stop()
            await interaction.response.send_message("⏭️ スキップしました。", ephemeral=True)
        else:
            await interaction.response.send_message("再生していません。", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_idle_task(interaction.guild.id)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        if interaction.guild.id in guild_states:
            del guild_states[interaction.guild.id]
        await interaction.response.send_message("⏹️ 停止しました。", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        order = {"off": "song", "song": "queue", "queue": "off"}
        state.loop_mode = order.get(state.loop_mode, "off")
        labels = {"off": "オフ", "song": "1曲リピート", "queue": "キュー全体リピート"}
        await interaction.response.send_message(
            f"🔁 リピート: **{labels[state.loop_mode]}**", ephemeral=True
        )

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        if len(state.queue) < 2:
            await interaction.response.send_message("シャッフルする曲が足りません。", ephemeral=True)
            return
        random.shuffle(state.queue)
        await interaction.response.send_message(
            f"🔀 キュー（{len(state.queue)}曲）をシャッフルしました。", ephemeral=True
        )


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
            embed = create_now_playing_embed(song)
            channel_id = song.get("text_channel_id")
            text_channel = vc.channel.guild.get_channel(channel_id) if channel_id else None
            if not text_channel:
                text_channel = vc.channel.guild.text_channels[0]
            asyncio.create_task(text_channel.send(embed=embed, view=MusicControls(guild_id)))
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
        audio_source = song.get("audio_url")
        # -reconnect options only apply to network input; local files reject them.
        before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

        if song.get("is_niconico"):
            local_file = await asyncio.get_event_loop().run_in_executor(
                None, download_niconico_audio, song["url"]
            )
            if not local_file:
                logger.error("Failed to download niconico audio")
                await play_next(guild_id)
                return
            song["local_file"] = local_file
            audio_source = local_file
            before_opts = None

        source = discord.FFmpegOpusAudio(
            audio_source,
            before_options=before_opts,
            options="-c:a libopus -b:a 192k -ar 48000 -ac 2",
        )
        vc.play(source, after=after_play)
    except Exception as e:
        logger.error(f"Play failed: {e}")
        await play_next(guild_id)


def download_niconico_audio(url: str) -> str:
    """Download niconico audio to a temp file and return the path."""
    tmpdir = tempfile.gettempdir()
    output_template = os.path.join(tmpdir, "nico_%(id)s.%(ext)s")

    ydl_opts = build_ydl_opts(url, outtmpl=output_template)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if os.path.exists(filename):
                logger.info(f"Downloaded niconico audio: {filename}")
                return filename
    except Exception as e:
        logger.error(f"Failed to download niconico audio: {e}")
    return None


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Sync error: {e}")
    bot.loop.create_task(background_cookie_refresh())


async def background_cookie_refresh():
    await asyncio.sleep(2)
    while True:
        try:
            async with cookie_refresh_lock:
                await asyncio.get_event_loop().run_in_executor(None, refresh_nico_cookies_sync, True)
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

    logger.info(f"Restarting song from beginning: {song['title']}")

    def after_restart(error):
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
        # The restarted song finished — advance the queue like a normal track end.
        # Previously the queue stalled here (next song never played after a sound effect).
        asyncio.run_coroutine_threadsafe(advance_queue(guild_id, song), bot.loop)

    try:
        audio_source = song.get("audio_url")
        before_opts = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"

        if song.get("is_niconico") and not song.get("local_file"):
            local_file = await asyncio.get_event_loop().run_in_executor(
                None, download_niconico_audio, song["url"]
            )
            if not local_file:
                logger.error("Failed to download niconico audio for restart")
                state.is_playing_sound = False
                return
            song["local_file"] = local_file
            audio_source = local_file
            before_opts = None
        elif song.get("local_file"):
            audio_source = song["local_file"]
            before_opts = None

        source = discord.FFmpegOpusAudio(
            audio_source,
            before_options=before_opts,
            options="-c:a libopus -b:a 192k -ar 48000 -ac 2",
        )
        vc.play(source, after=after_restart)
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

    loop = asyncio.get_event_loop()
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

    if vc.is_playing():
        embed = create_queued_embed(song, len(state.queue))
        await interaction.followup.send(embed=embed)
    else:
        state.current_song = song
        embed = create_now_playing_embed(song)
        await interaction.followup.send(embed=embed, view=MusicControls(interaction.guild.id))
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
    embed.add_field(name="/remove <番号>", value="キューから曲を削除", inline=True)
    embed.add_field(name="/clear", value="キューをクリア（再生は継続）", inline=True)
    embed.add_field(name="/join・/leave", value="VCに参加・退出", inline=True)
    embed.add_field(name="/na-", value="効果音（同一曲中1回）", inline=True)
    embed.add_field(name="/refresh", value="ニコニコCookie更新", inline=True)
    embed.add_field(name="メッセージトリガー", value="`んあー` / `んあーと` で効果音", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stop", description="Stop playing and clear the queue")
async def stop(interaction: discord.Interaction):
    cancel_idle_task(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
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
    embed = create_now_playing_embed(state.current_song)
    await interaction.response.send_message(embed=embed, view=MusicControls(interaction.guild.id))


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
    success = await asyncio.get_event_loop().run_in_executor(None, refresh_nico_cookies_sync, True)
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
