"""Queue advancement, playback lifecycle, idle disconnect, and the now-playing
progress-bar updater. Sits above audio + ui; imported by cog and bot."""
import asyncio
import os
import random
import time
from typing import Any, Dict, Optional

import discord

from .audio import (cleanup_download, current_elapsed, download_audio,
                    make_audio_source, reapply_audio_settings, related_songs)
from .config import (AUTOPLAY_BATCH, AUTOPLAY_MAX_STREAK, DOWNLOAD_TIMEOUT,
                     EFFECT_DEBOUNCE, NP_UPDATE_INTERVAL,
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


def active_download_paths() -> set:
    """Every temp file some guild is currently playing or holding for later.

    The periodic temp sweep is age-based, so a track playing longer than the
    sweep's max_age would otherwise be deleted mid-playback.
    """
    paths = set()
    for state in list(guild_states.values()):
        songs = ([state.current_song] if state.current_song else []) + list(state.queue)
        if state.prefetch_song:
            songs.append(state.prefetch_song)
        for song in songs:
            path = song.get("local_file")
            if path:
                paths.add(path)
    return paths


def cancel_prefetch(state: GuildState) -> None:
    task = state.prefetch_task
    state.prefetch_task = None
    state.prefetch_song = None
    if task and not task.done():
        task.cancel()


def _discard_stale_prefetch_files(
    state: GuildState, keep: Optional[Dict[str, Any]] = None,
) -> None:
    """Keep at most one downloaded queue-head file for this guild.

    A completed prefetch no longer has a live task. If the queue is then
    shuffled or moved, simply retargeting the task can otherwise leave the
    old head's file cached while downloading another one.
    """
    for queued_song in state.queue:
        if queued_song is keep:
            continue
        path = queued_song.get("local_file")
        if path:
            cleanup_download(path)
            queued_song["local_file"] = None


def start_prefetch(guild_id: int) -> None:
    """Download at most the next queued track while the current one plays."""
    state = guild_states.get(guild_id)
    if state is None or not state.queue:
        if state is not None:
            cancel_prefetch(state)
        return
    song = state.queue[0]
    _discard_stale_prefetch_files(state, keep=song)
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
            try:
                file_size = os.path.getsize(path) if path else 0
            except OSError:
                cleanup_download(path)
                return
            active = guild_states.get(guild_id)
            if (not path or active is not state
                    or (not any(item is target for item in state.queue)
                        and state.current_song is not target)
                    or not os.path.isfile(path)
                    or file_size > PREFETCH_MAX_BYTES):
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


def _human_listeners(state: GuildState) -> int:
    """Count non-bot members in the bot's voice channel."""
    vc = state.voice_client
    channel = getattr(vc, "channel", None) if vc is not None else None
    members = getattr(channel, "members", None) or []
    return sum(1 for member in members if not getattr(member, "bot", False))


def _autoplay_allowed(state: GuildState) -> bool:
    """Autoplay only for an occupied VC, and only up to the streak cap.

    Without both guards a queue that nobody is listening to would keep the bot
    connected indefinitely, since a refilled queue never reaches the idle
    disconnect branch.
    """
    if not state.autoplay:
        return False
    if state.autoplay_streak >= AUTOPLAY_MAX_STREAK:
        logger.info("Autoplay streak cap reached, falling back to idle disconnect")
        return False
    return _human_listeners(state) > 0


def _autoplay_fallback(guild_id: int, exclude: set) -> list:
    """Re-draw from the guild's own history when no related track is usable."""
    history = persistence.load_history(guild_id, 200)
    pool = {}
    for song in history:
        url = song.get("url")
        if url and url not in exclude:
            pool.setdefault(url, song)
    if not pool:
        return []
    picks = list(pool.values())
    random.shuffle(picks)
    return picks[:AUTOPLAY_BATCH]


async def collect_autoplay_songs(guild_id: int, state: GuildState) -> list:
    """Pick the next autoplay tracks: YouTube mix first, history second.

    Never raises: a failed suggestion must leave the drain path free to fall
    through to the normal idle disconnect.
    """
    seed = state.autoplay_seed or state.current_song
    # Recently played tracks are excluded so the radio doesn't loop a handful
    # of songs; the queue is checked too because a refill can race a /play.
    exclude = {song.get("url") for song in persistence.load_history(guild_id, 30)}
    exclude.update(song.get("url") for song in state.queue)
    if state.current_song:
        exclude.add(state.current_song.get("url"))
    exclude.discard(None)

    songs = []
    seed_url = (seed or {}).get("url")
    if seed_url:
        loop = asyncio.get_running_loop()
        try:
            songs = await asyncio.wait_for(
                loop.run_in_executor(
                    None, related_songs, seed_url, guild_id, AUTOPLAY_BATCH * 4),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Autoplay suggestion lookup timed out")
            songs = []
        except Exception as e:
            logger.warning(f"Autoplay suggestion lookup failed: {e}")
            songs = []
        songs = [song for song in songs if song.get("url") not in exclude]
        songs = songs[:AUTOPLAY_BATCH]
    if not songs:
        songs = _autoplay_fallback(guild_id, exclude)

    channel_id = (seed or {}).get("text_channel_id")
    prepared = []
    for song in songs:
        song = dict(song)
        song["autoplay"] = True
        song["requester"] = "オートDJ"
        song.pop("requester_id", None)
        song["local_file"] = None
        if channel_id is not None:
            song["text_channel_id"] = channel_id
        prepared.append(song)
    return prepared


async def advance_queue(guild_id: int, finished_song: Dict[str, Any],
                        expected_state: Optional[GuildState] = None,
                        failed: bool = False,
                        generation: Optional[int] = None) -> None:
    """Decide what to enqueue next based on loop/skip state, then play.

    `generation` is the playback generation the caller was registered for. A
    threaded `after` callback can reach the loop after a newer song has already
    started (e.g. a /play that saw the VC idle), so a stale generation must not
    clear current_song or advance the queue on the new playback's behalf.
    """
    if expected_state is not None:
        if guild_states.get(guild_id) is not expected_state:
            return
        state = expected_state  # avoid get_state() resurrecting a dropped guild
    else:
        state = get_state(guild_id)
    async with state.lock:
        if guild_states.get(guild_id) is not state:
            return
        if generation is not None and generation != state.play_generation:
            # A newer playback owns the state. The song did finish, so it still
            # belongs in the history, but nothing else here may be touched.
            logger.debug(
                f"Ignoring stale playback callback (gen {generation} != "
                f"{state.play_generation}): {finished_song.get('title')}")
            if not failed and state.persistence_hydrated:
                persistence.record_history(guild_id, finished_song)
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

    autoplay_attempts = 0
    while True:
        refill = False
        async with state.lock:
            if guild_states.get(guild_id) is not state:
                return
            if not state.queue:
                # Queue drained (empty from the start, or every remaining
                # song failed). Autoplay gets a chance to refill it first; one
                # empty refill ends the attempts so this can't spin.
                refill = autoplay_attempts < 1 and _autoplay_allowed(state)
                if not refill:
                    state.current_song = None
                    await retire_now_playing(state)
                    persist_queue(state)
                    logger.info(f"Queue empty, scheduling disconnect in {state.idle_timeout}s")
                    cancel_idle_task(guild_id)
                    state.idle_task = asyncio.create_task(schedule_disconnect(guild_id))
                    return
            else:
                song = state.queue.pop(0)
                state.current_song = song
                state.sound_used = False
                state.dispatching = True
                state.autoplay_seed = song
                state.autoplay_streak = (
                    state.autoplay_streak + 1 if song.get("autoplay") else 0)
                persist_queue(state)
                logger.info(f"Playing: {song['title']}")

        if refill:
            # Extraction is blocking and slow, so it runs without the lock.
            autoplay_attempts += 1
            songs = await collect_autoplay_songs(guild_id, state)
            async with state.lock:
                if guild_states.get(guild_id) is not state:
                    return
                if songs and not state.queue:
                    state.queue.extend(songs)
                    persist_queue(state)
                    logger.info(f"Autoplay queued {len(songs)} related track(s)")
            continue

        # Capture the running loop so the threaded `after` callback can hop back.
        loop = asyncio.get_running_loop()
        # Claim this dispatch's generation up front so the callback below can
        # prove it belongs to the playback that is still current. Claiming it
        # before the download is deliberate: a dispatch that then fails leaves
        # the counter ahead, which only ever invalidates older callbacks.
        generation = state.next_play_generation()

        def after_play(error, song=song, generation=generation):
            if error:
                logger.error(f"Play error: {error}")
            if state.is_playing_sound:
                # A sound effect stopped this source deliberately; the song is
                # about to resume from the same file. Deleting it here forced a
                # full re-download (seconds of silence, sometimes a skip).
                return
            cleanup_download(song.get("local_file"))
            song["local_file"] = None
            if guild_states.get(guild_id) is state:
                async def _finish() -> None:
                    if error:
                        await notify_skip(
                            guild_id, song, "再生中のエラー", expected_state=state)
                    await advance_queue(
                        guild_id, song, expected_state=state,
                        failed=bool(error), generation=generation)
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
            # The song is already popped, so cleanup_guild_state won't see it:
            # drop its download here or the dl_* dir leaks until the sweep.
            cleanup_download(song.get("local_file"))
            song["local_file"] = None
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
    # The generation this resume belongs to. Re-playing below claims a fresh
    # one (the old FFmpeg source is gone), and `finish` reads whichever is
    # current at call time — the entry one on the skip path below, the new one
    # once vc.play() has run.
    generation = state.play_generation

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
                    guild_id, song, expected_state=state, failed=bool(error),
                    generation=generation), loop
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
                await advance_queue(
                    guild_id, song, expected_state=state, generation=generation)
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
                await advance_queue(
                    guild_id, song, expected_state=state, generation=generation)
                return
            song["local_file"] = local_file

        # A /play racing the effect sees an idle VC, so a newer song may have
        # taken over during the sleep/download above; the generation says so.
        if (guild_states.get(guild_id) is not state
                or state.voice_client is not vc or not vc.is_connected()
                or state.play_generation != generation
                or vc.is_playing() or vc.is_paused()):
            cleanup_download(song.get("local_file"))
            song["local_file"] = None
            state.is_playing_sound = False
            return

        # Resume from where the sound effect interrupted, keeping speed/pitch.
        generation = state.next_play_generation()
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
        cleanup_download(song.get("local_file"))
        song["local_file"] = None
        state.skip_flag = True  # forced skip: don't let advance_queue re-loop this song
        await notify_skip(guild_id, song, "再開失敗", expected_state=state)
        await advance_queue(
            guild_id, song, expected_state=state, generation=generation)


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
