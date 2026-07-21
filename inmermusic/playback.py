"""Queue advancement, playback lifecycle, idle disconnect, and the now-playing
progress-bar updater. Sits above audio + ui; imported by cog and bot."""
import asyncio
import os
import time
from typing import Any, Dict, Optional

import discord

from .audio import (current_elapsed, download_audio, make_audio_source,
                    reapply_audio_settings)
from .config import (DOWNLOAD_TIMEOUT, EFFECT_DEBOUNCE, IDLE_TIMEOUT,
                     NP_UPDATE_INTERVAL, logger)
from .state import GuildState, get_state, guild_states
from .ui import MusicControls, create_now_playing_embed
from .util import fmt_duration


def cancel_idle_task(guild_id: int) -> None:
    state = guild_states.get(guild_id)
    if state is None:
        return
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
            cleanup_guild_state(guild_id)
    except asyncio.CancelledError:
        pass


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
                    await msg.edit(embed=embed, view=MusicControls())
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
        await msg.edit(embed=embed, view=MusicControls())
    except Exception as e:
        logger.warning(f"Failed to refresh now playing message: {e}")


def cancel_reapply(state: GuildState) -> None:
    """Cancel a pending debounced source swap, if any."""
    if state.reapply_task and not state.reapply_task.done():
        state.reapply_task.cancel()
    state.reapply_task = None


def schedule_reapply(guild_id: int) -> None:
    """Debounce in-place re-rendering: a burst of effect/speed/pitch/volume
    changes (button mashing) triggers a single FFmpeg source swap after a short
    quiet window instead of one per press. The state change is applied by the
    caller immediately; only the (expensive, race-prone) swap is deferred."""
    state = get_state(guild_id)
    cancel_reapply(state)

    async def _run():
        try:
            await asyncio.sleep(EFFECT_DEBOUNCE)
        except asyncio.CancelledError:
            return
        state.reapply_task = None
        vc = state.voice_client
        if (vc and vc.is_connected() and (vc.is_playing() or vc.is_paused())
                and not state.is_playing_sound):
            reapply_audio_settings(vc, state)

    state.reapply_task = asyncio.create_task(_run())


def resolve_text_channel(guild: discord.Guild, song: Dict[str, Any]) -> Optional[discord.abc.GuildChannel]:
    """Resolve the text channel a song's /play was issued from, falling back
    to the first channel the bot can actually send in. Shared by play_next
    and on_voice_state_update so channel-resolution logic lives in one place."""
    channel_id = song.get("text_channel_id")
    text_channel = guild.get_channel(channel_id) if channel_id else None
    if not text_channel:
        text_channel = next(
            (ch for ch in guild.text_channels
             if ch.permissions_for(guild.me).send_messages),
            None,
        )
    return text_channel


async def announce_now_playing(guild_id: int) -> None:
    """Send the now-playing message for the guild's current song and start
    its progress-bar updater. Only call once vc.play() has actually started."""
    state = get_state(guild_id)
    vc = state.voice_client
    song = state.current_song
    if not vc or not song:
        return
    try:
        embed = create_now_playing_embed(song, elapsed=0.0, state=state)
        text_channel = resolve_text_channel(vc.channel.guild, song)
        if text_channel:
            state.np_message = await text_channel.send(embed=embed, view=MusicControls())
            start_np_updater(guild_id)
    except Exception as e:
        logger.warning(f"Failed to send now playing message: {e}")


async def notify_skip(guild_id: int, song: Dict[str, Any], reason: str) -> None:
    """Tell the channel a queued song couldn't be played and was skipped."""
    state = get_state(guild_id)
    vc = state.voice_client
    if not vc:
        return
    text_channel = resolve_text_channel(vc.channel.guild, song)
    if text_channel:
        try:
            await text_channel.send(
                f"⚠️ **{song['title']}** を再生できませんでした（{reason}）。スキップします。"
            )
        except Exception as e:
            logger.warning(f"Failed to send skip notice: {e}")


def cleanup_guild_state(guild_id: int) -> None:
    """Cancel every background task for a guild and drop its state.

    Shared teardown for /leave, /stop, the idle-timeout disconnect,
    on_voice_state_update (empty VC), and on_guild_remove. Call after
    vc.stop() / await vc.disconnect().
    """
    state = guild_states.get(guild_id)
    if state is None:
        return
    cancel_idle_task(guild_id)
    cancel_np_updater(state)
    cancel_reapply(state)  # previously never cancelled at teardown
    state.np_message = None
    guild_states.pop(guild_id, None)


def mark_paused(state: GuildState, vc: discord.VoiceClient) -> None:
    if not state.clock_paused:
        state.paused_position = current_elapsed(vc, state)
        state.clock_paused = True


def mark_resumed(state: GuildState) -> None:
    state.clock_base = state.paused_position
    state.clock_speed = state.speed
    state.clock_started_at = time.monotonic()
    state.clock_paused = False


async def advance_queue(guild_id: int, finished_song: Dict[str, Any],
                        expected_state: Optional[GuildState] = None) -> None:
    """Decide what to enqueue next based on loop/skip state, then play."""
    if expected_state is not None and guild_states.get(guild_id) is not expected_state:
        return
    state = get_state(guild_id)
    async with state.lock:
        if expected_state is not None and guild_states.get(guild_id) is not state:
            return
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
    async with state.lock:
        await _play_next(guild_id, state, announce)


async def _play_next(guild_id: int, state: GuildState, announce: bool = True) -> None:
    if guild_states.get(guild_id) is not state:
        return
    vc = state.voice_client

    if not vc or not vc.is_connected():
        return
    if vc.is_playing() or vc.is_paused():
        return

    cancel_idle_task(guild_id)

    while state.queue:
        song = state.queue.pop(0)
        state.current_song = song
        state.sound_used = False
        logger.info(f"Playing: {song['title']}")

        # Capture the running loop so the threaded `after` callback can hop back.
        loop = asyncio.get_running_loop()

        def after_play(error, song=song):
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
            if not state.is_playing_sound and guild_states.get(guild_id) is state:
                asyncio.run_coroutine_threadsafe(
                    advance_queue(guild_id, song, expected_state=state), loop
                )

        try:
            if song.get("needs_local") and not song.get("local_file"):
                try:
                    local_file = await asyncio.wait_for(
                        loop.run_in_executor(None, download_audio, song["url"]),
                        timeout=DOWNLOAD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Download timed out after {DOWNLOAD_TIMEOUT}s, skipping: {song['title']}")
                    await notify_skip(guild_id, song, "読み込みタイムアウト")
                    continue
                if not local_file:
                    logger.error("Failed to download audio")
                    await notify_skip(guild_id, song, "読み込み失敗")
                    continue
                song["local_file"] = local_file

            # /stop or an external VC disconnect may have removed this state
            # while yt-dlp was running. Never attach the completed download to
            # a dead voice client or a new playback session.
            if (guild_states.get(guild_id) is not state
                    or state.voice_client is not vc or not vc.is_connected()):
                local_file = song.get("local_file")
                if local_file and os.path.exists(local_file):
                    try:
                        os.remove(local_file)
                    except OSError:
                        pass
                song["local_file"] = None
                return

            # A fresh FFmpeg process starts at loops=0; reset the seek bookkeeping.
            state.seek_position = 0.0
            state.loops_at_swap = 0
            state.speed_at_swap = state.speed
            source = make_audio_source(song, state, seek=0.0)
            vc.play(source, after=after_play)
            state.clock_base = 0.0
            state.clock_speed = state.speed
            state.clock_started_at = time.monotonic()
            state.clock_paused = False
            state.paused_position = 0.0
        except Exception as e:
            logger.error(f"Play failed: {e}")
            await notify_skip(guild_id, song, "再生エラー")
            continue

        # Only announce/save the Now Playing message once vc.play() succeeded,
        # so a failed track never leaves a stale message behind.
        if announce:
            await announce_now_playing(guild_id)
        return

    # Queue drained (empty from the start, or every remaining song failed).
    state.current_song = None
    cancel_np_updater(state)
    state.np_message = None
    logger.info(f"Queue empty, scheduling disconnect in {IDLE_TIMEOUT}s")
    cancel_idle_task(guild_id)
    state.idle_task = asyncio.create_task(schedule_disconnect(guild_id))


async def restart_song(guild_id: int, expected_state: Optional[GuildState] = None) -> None:
    await asyncio.sleep(0.3)
    if expected_state is not None and guild_states.get(guild_id) is not expected_state:
        return
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

    # Capture the running loop so the threaded `finish` callback can hop back.
    loop = asyncio.get_running_loop()

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
        if guild_states.get(guild_id) is state:
            asyncio.run_coroutine_threadsafe(
                advance_queue(guild_id, song, expected_state=state), loop
            )

    # A skip requested during the sound effect should move on, not replay the song.
    if state.skip_flag:
        logger.info("Skip requested during sound effect; advancing instead of restarting")
        finish()
        return

    seek = max(0.0, state.resume_position)
    logger.info(f"Resuming song at {fmt_duration(seek)}: {song['title']}")

    try:
        if song.get("needs_local") and not song.get("local_file"):
            try:
                local_file = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        None, download_audio, song["url"]
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"Download timed out after {DOWNLOAD_TIMEOUT}s during restart: {song['title']}")
                await notify_skip(guild_id, song, "読み込みタイムアウト")
                await advance_queue(guild_id, song, expected_state=state)
                return
            if not local_file:
                logger.error("Failed to download audio for restart")
                await notify_skip(guild_id, song, "読み込み失敗")
                await advance_queue(guild_id, song, expected_state=state)
                return
            song["local_file"] = local_file

        if (guild_states.get(guild_id) is not state
                or state.voice_client is not vc or not vc.is_connected()):
            local_file = song.get("local_file")
            if local_file and os.path.exists(local_file):
                try:
                    os.remove(local_file)
                except OSError:
                    pass
            song["local_file"] = None
            state.is_playing_sound = False
            return

        # Resume from where the sound effect interrupted, keeping speed/pitch.
        state.seek_position = seek
        state.loops_at_swap = 0
        state.speed_at_swap = state.speed
        source = make_audio_source(song, state, seek=seek)
        vc.play(source, after=finish)
        state.is_playing_sound = False
        state.clock_base = seek
        state.clock_speed = state.speed
        state.clock_started_at = time.monotonic()
        state.clock_paused = False
        state.paused_position = seek
    except Exception as e:
        logger.error(f"Failed to restart song: {e}")
        state.is_playing_sound = False
        await notify_skip(guild_id, song, "再開失敗")
        await advance_queue(guild_id, song, expected_state=state)


def play_sound_effect(guild_id: int, sound_path: str) -> bool:
    """Interrupt the current song with a one-shot sound effect, then resume it.

    Shared by /na-, /sound and the message triggers. Returns False if nothing
    is playing or a sound effect is already in progress. Must be called from a
    coroutine (it captures the running loop for the threaded `after` callback).
    """
    state = get_state(guild_id)
    vc = state.voice_client
    if not vc or not vc.is_connected() or not vc.is_playing():
        return False
    if state.is_playing_sound:
        return False
    if state.sound_used:
        return False

    loop = asyncio.get_running_loop()
    state.resume_position = current_elapsed(vc, state)  # resume here after the effect
    state.is_playing_sound = True
    state.sound_used = True
    vc.stop()

    def after_sound(error):
        if error:
            logger.error(f"Sound effect error: {error}")
        try:
            if guild_states.get(guild_id) is state:
                asyncio.run_coroutine_threadsafe(
                    restart_song(guild_id, expected_state=state), loop
                )
        except Exception as e:
            logger.error(f"Failed to schedule restart: {e}")
            state.is_playing_sound = False

    try:
        source = discord.FFmpegOpusAudio(
            sound_path,
            options="-c:a libopus -b:a 192k -ar 48000 -ac 2",
        )
        vc.play(source, after=after_sound)
        return True
    except Exception as e:
        logger.error(f"Failed to play sound: {e}")
        # The original song has already been stopped; use the same recovery
        # path as a normally finished sound effect.
        if guild_states.get(guild_id) is state:
            asyncio.run_coroutine_threadsafe(
                restart_song(guild_id, expected_state=state), loop
            )
        return False
