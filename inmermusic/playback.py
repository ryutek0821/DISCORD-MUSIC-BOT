"""Queue advancement, playback lifecycle, idle disconnect, and the now-playing
progress-bar updater. Sits above audio + ui; imported by cog and bot."""
import asyncio
import os
import time
from typing import Any, Dict, Optional

import discord

from .audio import (cleanup_download, current_elapsed, download_audio,
                    make_audio_source, reapply_audio_settings)
from .config import (DOWNLOAD_TIMEOUT, EFFECT_DEBOUNCE, NP_UPDATE_INTERVAL,
                     PREFETCH_MAX_BYTES, logger)
from . import persistence
from .state import GuildState, get_state, guild_states
from .ui import MusicControls, create_now_playing_embed
from .util import fmt_duration, short_extract_error


def cancel_idle_task(guild_id: int) -> None:
    state = guild_states.get(guild_id)
    if state is None:
        return
    if state.idle_task:
        state.idle_task.cancel()
        state.idle_task = None


async def schedule_disconnect(guild_id: int) -> None:
    try:
        state = guild_states.get(guild_id)
        timeout = state.idle_timeout if state is not None else 180
        await asyncio.sleep(timeout)
        # guild_states.get, never get_state: a task that outlived its guild
        # must not resurrect a dropped GuildState just to check for idleness.
        state = guild_states.get(guild_id)
        if state is None:
            return
        vc = state.voice_client
        if vc and vc.is_connected() and not vc.is_playing():
            logger.info(f"Idle timeout ({timeout}s), disconnecting")
            await vc.disconnect()
            cleanup_guild_state(guild_id)
    except asyncio.CancelledError:
        pass


def cancel_np_updater(state: GuildState) -> None:
    """Stop the running now-playing progress-bar refresh loop, if any."""
    if state.np_updater is not None:
        state.np_updater.cancel()
        state.np_updater = None


async def retire_now_playing(state: GuildState) -> None:
    """Make the previous controller inert before advancing to another track."""
    cancel_np_updater(state)
    message = state.np_message
    state.np_message = None
    if message is not None:
        try:
            await message.edit(view=None)
        except Exception as e:
            logger.debug(f"Failed to retire old now-playing panel: {e}")


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
        if state.np_message is not None:
            await retire_now_playing(state)
        embed = create_now_playing_embed(song, elapsed=0.0, state=state)
        text_channel = resolve_text_channel(vc.channel.guild, song)
        if text_channel:
            state.np_message = await text_channel.send(embed=embed, view=MusicControls())
            start_np_updater(guild_id)
    except Exception as e:
        logger.warning(f"Failed to send now playing message: {e}")


async def notify_skip(guild_id: int, song: Dict[str, Any], reason: str,
                      expected_state: Optional[GuildState] = None) -> None:
    """Tell the channel a queued song couldn't be played and was skipped.

    Uses guild_states.get() (never get_state()) and honors expected_state so
    a callback racing a cleanup_guild_state() can't resurrect a dropped
    GuildState just to send a skip notice.
    """
    state = guild_states.get(guild_id)
    if state is None:
        return
    if expected_state is not None and state is not expected_state:
        return
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


def _cleanup_late_download(fut: "asyncio.Future") -> None:
    """Done-callback for a download future abandoned by a wait_for timeout.

    asyncio.wait_for can't stop the executor thread, so the download keeps
    running and eventually writes a file nobody will claim. Once it finishes,
    remove its temp directory so timed-out downloads don't accumulate.

    Only ever attached to a future that was wrapped in asyncio.shield() (see
    the download call sites): wait_for cancels its argument before raising
    TimeoutError, so without the shield the future would already be cancelled
    here and this would fire immediately with nothing to clean up. Catches
    BaseException because a cancelled future raises CancelledError, which is
    not an Exception subclass, and a done-callback must never leak into the
    event loop's exception handler.
    """
    try:
        result = fut.result()
    except BaseException:
        return
    path, _ = _download_parts(result)
    cleanup_download(path)


def _download_parts(result: Any) -> tuple[Optional[str], Optional[str]]:
    """Accept the structured result plus legacy string-returning test doubles."""
    if hasattr(result, "path"):
        return result.path, result.error
    return result, None


def persist_queue(state: GuildState) -> None:
    """Persist a crash-restorable snapshot; runtime file handles are stripped."""
    if state.guild_id is None or not state.persistence_hydrated:
        return
    songs = ([state.current_song] if state.current_song else []) + list(state.queue)
    persistence.save_queue(state.guild_id, songs)


def cancel_prefetch(state: GuildState) -> None:
    task = state.prefetch_task
    state.prefetch_task = None
    state.prefetch_song = None
    if task and not task.done():
        task.cancel()


def start_prefetch(guild_id: int) -> None:
    """Download at most the next queued track while the current one plays."""
    state = guild_states.get(guild_id)
    if state is None or not state.queue:
        if state is not None:
            cancel_prefetch(state)
        return
    song = state.queue[0]
    if not song.get("needs_local") or song.get("local_file"):
        cancel_prefetch(state)
        return
    if state.prefetch_song is song and state.prefetch_task and not state.prefetch_task.done():
        return
    cancel_prefetch(state)

    async def _run(target: Dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(None, download_audio, target["url"], guild_id)
        try:
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=DOWNLOAD_TIMEOUT)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                fut.add_done_callback(_cleanup_late_download)
                return
            except Exception as e:
                logger.warning(f"Next-track prefetch failed: {e}")
                return
            path, _ = _download_parts(result)
            active = guild_states.get(guild_id)
            if (not path or active is not state
                    or (not any(item is target for item in state.queue)
                        and state.current_song is not target)
                    or (os.path.getsize(path) if os.path.exists(path) else 0) > PREFETCH_MAX_BYTES):
                cleanup_download(path)
                return
            target["local_file"] = path
            logger.info(f"Prefetched: {target.get('title', 'Unknown')}")
        finally:
            if state.prefetch_song is target:
                state.prefetch_task = None
                state.prefetch_song = None

    state.prefetch_song = song
    state.prefetch_task = asyncio.create_task(_run(song))


async def _claim_prefetch(state: GuildState, song: Dict[str, Any]) -> None:
    task = state.prefetch_task
    if state.prefetch_song is not song or task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        cancel_prefetch(state)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def cleanup_guild_state(guild_id: int, *, clear_persisted: bool = True) -> None:
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
    cancel_prefetch(state)
    for song in (([state.current_song] if state.current_song else []) + state.queue):
        cleanup_download(song.get("local_file"))
        song["local_file"] = None
    if clear_persisted and state.persistence_hydrated:
        persistence.save_queue(guild_id, [])
    else:
        persist_queue(state)
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
                        expected_state: Optional[GuildState] = None,
                        failed: bool = False) -> None:
    """Decide what to enqueue next based on loop/skip state, then play."""
    if expected_state is not None:
        if guild_states.get(guild_id) is not expected_state:
            return
        state = expected_state  # avoid get_state() resurrecting a dropped guild
    else:
        state = get_state(guild_id)
    async with state.lock:
        if guild_states.get(guild_id) is not state:
            return
        state.current_song = None
        if not failed and state.persistence_hydrated:
            persistence.record_history(guild_id, finished_song)
        if failed or state.skip_flag:
            # Manual skip overrides loop: drop the finished song and move on.
            state.skip_flag = False
        elif state.loop_mode == "song":
            finished_song["local_file"] = None  # temp file already cleaned up
            state.queue.insert(0, finished_song)
        elif state.loop_mode == "queue":
            finished_song["local_file"] = None
            state.queue.append(finished_song)
        persist_queue(state)
    await retire_now_playing(state)
    await play_next(guild_id)


async def play_next(guild_id: int, announce: bool = True) -> None:
    state = get_state(guild_id)
    await _play_next(guild_id, state, announce)


async def _play_next(guild_id: int, state: GuildState, announce: bool = True) -> None:
    """Pop and play the next queued song, draining the queue on failure.

    The lock is released while a download is in flight, so a concurrent
    /play for the same guild doesn't block on someone else's up-to-
    DOWNLOAD_TIMEOUT download. `state.dispatching` fills the gap that leaves:
    it marks "a song has been popped and is being started" so a second
    concurrent call can't also pop before the first has actually called
    vc.play(). Every invariant that could change while unlocked is
    re-checked after each re-acquire.
    """
    async with state.lock:
        if guild_states.get(guild_id) is not state:
            return
        vc = state.voice_client
        if not vc or not vc.is_connected():
            return
        if vc.is_playing() or vc.is_paused():
            return
        if state.dispatching:
            # Another play_next call for this guild already popped a song and
            # is downloading/starting it; let it finish instead of racing to
            # pop a second one.
            return
        cancel_idle_task(guild_id)

    while True:
        async with state.lock:
            if guild_states.get(guild_id) is not state:
                return
            if not state.queue:
                # Queue drained (empty from the start, or every remaining
                # song failed).
                state.current_song = None
                await retire_now_playing(state)
                persist_queue(state)
                logger.info(f"Queue empty, scheduling disconnect in {state.idle_timeout}s")
                cancel_idle_task(guild_id)
                state.idle_task = asyncio.create_task(schedule_disconnect(guild_id))
                return
            song = state.queue.pop(0)
            state.current_song = song
            state.sound_used = False
            state.dispatching = True
            persist_queue(state)
            logger.info(f"Playing: {song['title']}")

        # Capture the running loop so the threaded `after` callback can hop back.
        loop = asyncio.get_running_loop()

        def after_play(error, song=song):
            if error:
                logger.error(f"Play error: {error}")
            cleanup_download(song.get("local_file"))
            song["local_file"] = None
            if not state.is_playing_sound and guild_states.get(guild_id) is state:
                async def _finish() -> None:
                    if error:
                        await notify_skip(
                            guild_id, song, "再生中のエラー", expected_state=state)
                    await advance_queue(
                        guild_id, song, expected_state=state, failed=bool(error))
                asyncio.run_coroutine_threadsafe(
                    _finish(), loop
                )

        try:
            if song.get("needs_local") and not song.get("local_file"):
                await _claim_prefetch(state, song)
            if song.get("needs_local") and not song.get("local_file"):
                fut = loop.run_in_executor(
                    None, download_audio, song["url"], guild_id)
                try:
                    # shield: wait_for cancels its argument before raising, so
                    # without it `fut` would already be cancelled below and the
                    # late-cleanup callback could never see the finished path.
                    download_result = await asyncio.wait_for(
                        asyncio.shield(fut), timeout=DOWNLOAD_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Download timed out after {DOWNLOAD_TIMEOUT}s, skipping: {song['title']}")
                    fut.add_done_callback(_cleanup_late_download)
                    await notify_skip(guild_id, song, "読み込みタイムアウト", expected_state=state)
                    state.dispatching = False
                    continue
                local_file, download_error = _download_parts(download_result)
                if not local_file:
                    logger.error("Failed to download audio")
                    reason = (
                        short_extract_error(download_error)
                        if download_error else "読み込み失敗"
                    )
                    await notify_skip(guild_id, song, reason, expected_state=state)
                    state.dispatching = False
                    continue
                song["local_file"] = local_file

            async with state.lock:
                # The lock was released for the download (if any); /stop, an
                # external VC disconnect, or another dispatcher racing us
                # (see `dispatching` above) may have changed things since.
                # Recheck every invariant before touching the voice client.
                if (guild_states.get(guild_id) is not state
                        or state.voice_client is not vc or not vc.is_connected()
                        or vc.is_playing() or vc.is_paused()):
                    cleanup_download(song.get("local_file"))
                    song["local_file"] = None
                    state.dispatching = False
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
                state.dispatching = False
                persist_queue(state)
        except Exception as e:
            logger.error(f"Play failed: {e}")
            await notify_skip(guild_id, song, "再生エラー", expected_state=state)
            state.dispatching = False
            continue

        # Only announce/save the Now Playing message once vc.play() succeeded,
        # so a failed track never leaves a stale message behind.
        if announce:
            await announce_now_playing(guild_id)
        start_prefetch(guild_id)
        return


async def restart_song(guild_id: int, expected_state: Optional[GuildState] = None) -> None:
    await asyncio.sleep(0.3)
    # expected_state is always passed by our only callers (play_sound_effect);
    # look the state up via guild_states.get (never get_state) so a callback
    # racing a cleanup_guild_state() can't resurrect a dropped GuildState.
    state = guild_states.get(guild_id)
    if state is None:
        return
    if expected_state is not None and state is not expected_state:
        return
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
        cleanup_download(song.get("local_file"))
        song["local_file"] = None
        # The song is done (finished or skipped) — advance the queue.
        if guild_states.get(guild_id) is state:
            asyncio.run_coroutine_threadsafe(
                advance_queue(
                    guild_id, song, expected_state=state, failed=bool(error)), loop
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
            fut = loop.run_in_executor(
                None, download_audio, song["url"], guild_id)
            try:
                # shield: see the matching call in _play_next.
                download_result = await asyncio.wait_for(
                    asyncio.shield(fut), timeout=DOWNLOAD_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.error(f"Download timed out after {DOWNLOAD_TIMEOUT}s during restart: {song['title']}")
                fut.add_done_callback(_cleanup_late_download)
                # A failed resume is a forced skip, not a normal end-of-song:
                # set skip_flag first so advance_queue won't reinsert this
                # song under loop_mode="song"/"queue" and retry the same
                # dead download forever.
                state.is_playing_sound = False
                state.skip_flag = True
                await notify_skip(guild_id, song, "読み込みタイムアウト", expected_state=state)
                await advance_queue(guild_id, song, expected_state=state)
                return
            local_file, download_error = _download_parts(download_result)
            if not local_file:
                logger.error("Failed to download audio for restart")
                state.is_playing_sound = False
                state.skip_flag = True
                reason = (
                    short_extract_error(download_error)
                    if download_error else "読み込み失敗"
                )
                await notify_skip(guild_id, song, reason, expected_state=state)
                await advance_queue(guild_id, song, expected_state=state)
                return
            song["local_file"] = local_file

        if (guild_states.get(guild_id) is not state
                or state.voice_client is not vc or not vc.is_connected()):
            cleanup_download(song.get("local_file"))
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
        state.skip_flag = True  # forced skip: don't let advance_queue re-loop this song
        await notify_skip(guild_id, song, "再開失敗", expected_state=state)
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
