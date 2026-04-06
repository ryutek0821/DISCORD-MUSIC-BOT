import os
import asyncio
import logging
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

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")

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

song_queues = {}
voice_clients_map = {}
current_song = {}
idle_tasks = {}
last_cookie_refresh = 0
COOKIE_TTL = 3600
cookie_refresh_lock = asyncio.Lock()
is_playing_sound = {}
song_start_time = {}

IDLE_TIMEOUT = 180

def get_queue(guild_id):
    if guild_id not in song_queues:
        song_queues[guild_id] = []
    return song_queues[guild_id]

def login_via_api():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    })
    session.get("https://account.nicovideo.jp/login")
    resp = session.post("https://account.nicovideo.jp/api/v1/login", data={
        "mail_tel": NICO_EMAIL,
        "password": NICO_PASSWORD,
    }, allow_redirects=True)
    logger.info(f"API login status: {resp.status_code}")
    return session.cookies

def save_session_cookies(cookies):
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
    logger.info(f"Saved {len(cookies)} session cookies")

def refresh_nico_cookies_sync(force=False):
    global last_cookie_refresh
    now = time.time()
    if not force and (now - last_cookie_refresh) < COOKIE_TTL:
        if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
            logger.info("Using cached cookies (not expired)")
            return True

    logger.info("Refreshing niconico cookies via API...")
    try:
        cookies = login_via_api()
        if len(cookies) >= 2:
            save_session_cookies(cookies)
            last_cookie_refresh = time.time()
            return True
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

        service = Service("/usr/bin/chromedriver")
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

def download_audio_file(url):
    tmpdir = tempfile.mkdtemp(prefix="niconico_bot_")

    if "nicovideo.jp" in url:
        refresh_nico_cookies_sync()

    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 10,
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
    }

    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        ydl_opts["cookiefile"] = COOKIE_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info and info["entries"]:
            info = info["entries"][0]
        filepath = ydl.prepare_filename(info)
        return {
            "filepath": filepath,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail", ""),
            "url": url,
        }

def cancel_idle_task(guild_id):
    if guild_id in idle_tasks:
        idle_tasks[guild_id].cancel()
        del idle_tasks[guild_id]

async def schedule_disconnect(guild_id):
    try:
        await asyncio.sleep(IDLE_TIMEOUT)
        vc = voice_clients_map.get(guild_id)
        if vc and vc.is_connected() and not vc.is_playing():
            logger.info(f"Idle timeout ({IDLE_TIMEOUT}s), disconnecting")
            await vc.disconnect()
            voice_clients_map.pop(guild_id, None)
            song_queues.pop(guild_id, None)
    except asyncio.CancelledError:
        pass

async def play_next(guild_id):
    queue = get_queue(guild_id)
    vc = voice_clients_map.get(guild_id)

    if not vc or not vc.is_connected():
        return

    if len(queue) == 0:
        current_song.pop(guild_id, None)
        logger.info(f"Queue empty, scheduling disconnect in {IDLE_TIMEOUT}s")
        cancel_idle_task(guild_id)
        idle_tasks[guild_id] = asyncio.create_task(schedule_disconnect(guild_id))
        return

    cancel_idle_task(guild_id)

    song = queue.pop(0)
    current_song[guild_id] = song
    logger.info(f"Playing: {song['title']}")

    def after_play(error):
        if error:
            logger.error(f"Play error: {error}")
        fp = song.get("filepath")
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except:
                pass
        asyncio.run_coroutine_threadsafe(play_next(guild_id), bot.loop)

    try:
        vc.play(
            discord.FFmpegPCMAudio(
                song["filepath"],
                options="-vn",
            ),
            after=after_play,
        )
    except Exception as e:
        logger.error(f"Play failed: {e}")
        fp = song.get("filepath")
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except:
                pass
        await play_next(guild_id)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Sync error: {e}")
    bot.loop.create_task(background_cookie_refresh())

@bot.event
async def on_message(message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    guild_id = message.guild.id
    content = message.content
    
    if content in ["んあー", "んあーと"]:
        if is_playing_sound.get(guild_id):
            return
        
        vc = voice_clients_map.get(guild_id)
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
        is_playing_sound[guild_id] = True
        
        was_playing = vc.is_playing()
        if was_playing:
            vc.pause()
        
        def after_sound(error):
            if error:
                logger.error(f"Sound effect error: {error}")
            asyncio.run_coroutine_threadsafe(
                resume_after_sound(guild_id), bot.loop
            )
        
        try:
            vc.play(discord.FFmpegPCMAudio(mp3_file), after=after_sound)
        except Exception as e:
            logger.error(f"Failed to play sound: {e}")
            is_playing_sound[guild_id] = False
            if was_playing:
                vc.resume()
    
    await bot.process_commands(message)

async def restart_song(guild_id):
    await asyncio.sleep(0.3)
    vc = voice_clients_map.get(guild_id)
    if not vc or not vc.is_connected():
        is_playing_sound[guild_id] = False
        song_position.pop(guild_id, None)
        return
    
    if not filepath or not os.path.exists(filepath):
        logger.warning(f"Song file not found for restart: {filepath}")
        is_playing_sound[guild_id] = False
        song_position.pop(guild_id, None)
        return
    
    song = current_song.get(guild_id)
    if not song:
        is_playing_sound[guild_id] = False
        song_position.pop(guild_id, None)
        return
    
    # Calculate position from elapsed time (subtract sound effect duration ~0.5s)
    position = int(time.time() - start_time) if start_time else 0
    
    logger.info(f"Restarting song after sound effect: {song['title']} from {position}s")
    
    def after_restart(error):
        if error:
            logger.error(f"Restart error: {error}")
        is_playing_sound[guild_id] = False
    
    try:
        ffmpeg_options = "-vn" + (f" -ss {position}" if position > 0 else "")
        vc.play(
            discord.FFmpegPCMAudio(
                filepath,
                options=ffmpeg_options,
            ),
            after=after_restart,
        )
    except Exception as e:
        logger.error(f"Failed to restart song: {e}")
        is_playing_sound[guild_id] = False

async def background_cookie_refresh():
    await asyncio.sleep(2)
    async with cookie_refresh_lock:
        await asyncio.get_event_loop().run_in_executor(None, refresh_nico_cookies_sync, True)

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
            loop.run_in_executor(None, download_audio_file, query),
            timeout=180
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("曲の取得がタイムアウトしました。")
        return
    except Exception as e:
        await interaction.followup.send(f"曲が見つかりません: {str(e)}")
        return

    if not vc:
        try:
            vc = await channel.connect(timeout=15)
            voice_clients_map[interaction.guild.id] = vc
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

    queue = get_queue(interaction.guild.id)
    queue.append(song)

    if vc.is_playing():
        embed = discord.Embed(
            title="キューに追加",
            description=f"**{song['title']}** をキューに追加しました (#{len(queue)})",
            color=0x00ff00,
        )
        if song["thumbnail"]:
            embed.set_thumbnail(url=song["thumbnail"])
        await interaction.followup.send(embed=embed)
    else:
        current_song[interaction.guild.id] = song
        duration_str = f"{song['duration'] // 60}:{song['duration'] % 60:02d}"
        embed = discord.Embed(
            title="再生中",
            description=f"**[{song['title']}]({song['url']})**\n再生時間: {duration_str}",
            color=0x00ff00,
        )
        if song["thumbnail"]:
            embed.set_thumbnail(url=song["thumbnail"])
        await interaction.followup.send(embed=embed)
        await play_next(interaction.guild.id)

@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        await interaction.response.send_message("再生していません。")
        return
    vc.stop()
    await interaction.response.send_message("スキップしました！")

@bot.tree.command(name="queue", description="Show the current queue")
async def queue_cmd(interaction: discord.Interaction):
    q = get_queue(interaction.guild.id)
    if not q:
        await interaction.response.send_message("キューは空です。")
        return
    desc = "\n".join(f"{i+1}. **{s['title']}**" for i, s in enumerate(q[:10]))
    embed = discord.Embed(title="キュー", description=desc, color=0x00ff00)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stop", description="Stop playing and clear the queue")
async def stop(interaction: discord.Interaction):
    cancel_idle_task(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
    song_queues.pop(interaction.guild.id, None)
    voice_clients_map.pop(interaction.guild.id, None)
    current_song.pop(interaction.guild.id, None)
    await interaction.response.send_message("停止してキューをクリアしました。")

@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("一時停止しました。")
    else:
        await interaction.response.send_message("再生していません。")

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
    song = current_song.get(interaction.guild.id)
    if not song:
        await interaction.response.send_message("再生中の曲はありません。")
        return
    duration_str = f"{song['duration'] // 60}:{song['duration'] % 60:02d}"
    embed = discord.Embed(
        title="再生中",
        description=f"**[{song['title']}]({song['url']})**\n再生時間: {duration_str}",
        color=0x00ff00,
    )
    if song["thumbnail"]:
        embed.set_thumbnail(url=song["thumbnail"])
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="na-", description="ンアッー!(≧д≦)")
async def na_command(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    
    vc = voice_clients_map.get(guild_id)
    if not vc or not vc.is_connected():
        await interaction.response.send_message("VCに接続していません。")
        return
    
    if not vc.is_playing():
        await interaction.response.send_message("再生していません。")
        return
    
    mp3_file = os.path.join(SOUNDS_DIR, "na-.mp3")
    if not os.path.exists(mp3_file):
        await interaction.response.send_message("効果音ファイルが見つかりません。")
        return
    
    if is_playing_sound.get(guild_id):
        await interaction.response.send_message("再生待ちです。")
        return
    
    logger.info("Playing sound effect via /na-")
    is_playing_sound[guild_id] = True
    
    song = current_song.get(guild_id)
    song_filepath = song["filepath"] if song else None
    
    # Store start time for position calculation
    song_start_time[guild_id] = {"filepath": song_filepath, "start": time.time()}
    
    vc.pause()
    
    def after_sound(error):
        if error:
            logger.error(f"Sound effect error: {error}")
        # Don't reset is_playing_sound here, let restart_song do it
    
    try:
        vc.play(discord.FFmpegPCMAudio(mp3_file), after=after_sound)
        await interaction.response.send_message("ンアッー!")
    except Exception as e:
        logger.error(f"Failed to play sound: {e}")
        is_playing_sound[guild_id] = False
        vc.resume()
        await interaction.response.send_message("効果音の再生に失敗しました。")

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

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Error: Please set DISCORD_TOKEN in .env file")
        exit(1)
    bot.run(TOKEN)
