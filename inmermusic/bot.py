"""Bot construction, startup wiring, and the run() entrypoint."""
import asyncio

import discord
from discord.ext import commands

from . import cookies, persistence
from .audio import cleanup_temp_files
from .cog import MusicCog
from .config import COOKIE_TTL, TEMP_SWEEP_INTERVAL, TOKEN, logger
from .playback import active_download_paths
from .ui import MusicControls

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True


async def background_cookie_refresh():
    await asyncio.sleep(2)
    while True:
        try:
            # Serialization with on-demand refreshes happens inside
            # refresh_nico_cookies_sync via cookie_refresh_lock (threading.Lock).
            await asyncio.get_running_loop().run_in_executor(None, cookies.refresh_nico_cookies_sync, True)
        except Exception as e:
            logger.error(f"Background cookie refresh error: {e}")
        await asyncio.sleep(COOKIE_TTL)


async def background_temp_sweep():
    """Reap orphaned downloads periodically, not just at startup.

    The bot is expected to stay up between deploys, so a startup-only sweep
    means a leaked `dl_*` directory survives until the next restart. Runs in an
    executor because listdir/rmtree over the download dir is blocking I/O.
    """
    while True:
        await asyncio.sleep(TEMP_SWEEP_INTERVAL)
        try:
            # Snapshot the in-use paths on the loop, where guild_states is
            # only mutated, then hand the blocking sweep to a worker.
            in_use = active_download_paths()
            await asyncio.get_running_loop().run_in_executor(
                None, cleanup_temp_files, 3600, in_use)
        except Exception as e:
            logger.error(f"Background temp sweep error: {e}")


class MusicBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(MusicCog(self))
        # Register the persistent control view so now-playing buttons keep
        # working after a restart.
        self.add_view(MusicControls())


bot = MusicBot(
    command_prefix="!", intents=intents,
    allowed_mentions=discord.AllowedMentions.none(),
)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    # on_ready fires again on every gateway reconnect; run startup work only
    # once. tree.sync() belongs inside this guard too — command definitions
    # can't change while the process lives, and the global sync endpoint is
    # rate-limited hard enough that a flapping connection would earn a 429.
    if getattr(bot, "_startup_done", False):
        return
    bot._startup_done = True
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} commands")
    except Exception as e:
        logger.error(f"Sync error: {e}")
    # Clear temp downloads orphaned by a previous crash, then keep sweeping.
    cleanup_temp_files()
    bot.loop.create_task(background_cookie_refresh())
    bot.loop.create_task(background_temp_sweep())


def run():
    """Validate the token and start the bot (blocking)."""
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Error: Please set DISCORD_TOKEN in .env file")
        raise SystemExit(1)
    try:
        bot.run(TOKEN)
    finally:
        # Queue snapshots are written off the loop; don't exit on top of one.
        persistence.flush_writes()
