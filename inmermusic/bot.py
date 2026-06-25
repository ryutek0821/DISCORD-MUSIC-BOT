"""Bot construction, startup wiring, and the run() entrypoint."""
import asyncio

import discord
from discord.ext import commands

from . import cookies
from .audio import cleanup_temp_files
from .cog import MusicCog
from .config import COOKIE_TTL, TOKEN, logger
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


class MusicBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(MusicCog(self))
        # Register the persistent control view so now-playing buttons keep
        # working after a restart.
        self.add_view(MusicControls())


bot = MusicBot(command_prefix="!", intents=intents)


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
        # Clear temp downloads orphaned by a previous crash.
        cleanup_temp_files()
        bot.loop.create_task(background_cookie_refresh())


def run():
    """Validate the token and start the bot (blocking)."""
    if not TOKEN or TOKEN == "your_discord_bot_token_here":
        print("Error: Please set DISCORD_TOKEN in .env file")
        raise SystemExit(1)
    bot.run(TOKEN)
