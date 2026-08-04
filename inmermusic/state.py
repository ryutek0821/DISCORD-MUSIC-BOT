"""Per-guild playback state and the in-memory registry."""
import asyncio
from typing import Any, Dict, List, Optional

import discord


class GuildState:
    """Manages per-guild playback state."""
    def __init__(self, guild_id: Optional[int] = None):
        self.guild_id = guild_id
        self.queue: List[Dict[str, Any]] = []
        self.restored_count: int = 0
        self.persistence_hydrated: bool = False
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_song: Optional[Dict[str, Any]] = None
        self.idle_task: Optional[asyncio.Task] = None
        self.is_playing_sound: bool = False
        self.sound_used: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()
        self.loop_mode: str = "off"
        self.skip_flag: bool = False
        self.speed: float = 1.0          # playback tempo (0.5–2.0), pitch preserved
        self.pitch: int = 0              # pitch shift in semitones (-12–+12)
        self.volume: int = 100
        self.default_volume: int = 100  # guild default; volume != this is a user tweak
        self.idle_timeout: int = 180
        self.autoplay: bool = False      # keep the queue fed with related tracks
        self.autoplay_streak: int = 0    # consecutive autoplay tracks; reset by any user request
        self.autoplay_seed: Optional[Dict[str, Any]] = None  # track related songs are drawn from
        self.effect: str = "off"         # active effect preset (see EFFECT_FILTERS)
        self.seek_position: float = 0.0  # start offset (s) of the current FFmpeg source
        self.loops_at_swap: int = 0      # player.loops captured when seek_position was set
        self.speed_at_swap: float = 1.0  # tempo active for the current source segment
        self.resume_position: float = 0.0  # song position to resume at after a sound effect
        # Monotonic playback clock.  discord.py resets its private player loop
        # counter on resume, so elapsed time must not depend on that counter.
        self.clock_started_at: Optional[float] = None
        self.clock_base: float = 0.0
        self.clock_speed: float = 1.0
        self.clock_paused: bool = False
        self.paused_position: float = 0.0
        self.np_message: Optional[discord.Message] = None  # live now-playing message
        self.np_updater: Optional[asyncio.Task] = None      # progress-bar refresh loop
        self.np_refresh_task: Optional[asyncio.Task] = None  # debounced panel edit
        self.reapply_task: Optional[asyncio.Task] = None    # debounced source-swap timer
        self.prefetch_task: Optional[asyncio.Task] = None   # next-track download
        self.prefetch_song: Optional[Dict[str, Any]] = None
        # True while _play_next has popped a song and is downloading/starting
        # it with state.lock released; guards against a second concurrent
        # play_next call (e.g. two racing /play commands) popping a second
        # song before the first has actually started playing.
        self.dispatching: bool = False
        # Generation of the playback dispatch that currently owns this state.
        # discord.py fires the `after` callback from the player thread, which
        # then hops back to the loop, so a callback can run long after a newer
        # song has taken over. Every callback carries the generation it was
        # registered for and is ignored unless it still matches.
        self.play_generation: int = 0

    def next_play_generation(self) -> int:
        """Claim the next playback generation, invalidating older callbacks."""
        self.play_generation += 1
        return self.play_generation


guild_states: Dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState(guild_id)
    return guild_states[guild_id]


def hydrate_state(guild_id: int) -> GuildState:
    """Load durable queue/preferences once at a user-command boundary."""
    state = get_state(guild_id)
    if state.persistence_hydrated:
        return state
    from . import persistence
    settings = persistence.get_settings(guild_id)
    if not state.queue and state.current_song is None:
        state.queue = persistence.load_queue(guild_id)
        state.restored_count = len(state.queue)
    state.volume = settings["default_volume"]
    state.default_volume = settings["default_volume"]
    state.idle_timeout = settings["idle_timeout"]
    state.loop_mode = settings["loop_mode"]
    state.autoplay = settings["autoplay"]
    state.persistence_hydrated = True
    return state


def is_idle(state: GuildState) -> bool:
    """True when a state holds nothing worth keeping in memory.

    Deliberately strict: anything the user set but hasn't persisted (speed,
    pitch, effect, a volume tweak) counts as "in use", because dropping the
    state would silently discard it. loop_mode/autoplay/idle_timeout live in
    the settings table, so they survive either way.
    """
    if state.voice_client is not None or state.current_song is not None:
        return False
    if state.queue or state.np_message is not None:
        return False
    if state.dispatching or state.is_playing_sound:
        return False
    if any(task is not None for task in (
            state.idle_task, state.np_updater, state.np_refresh_task,
            state.reapply_task, state.prefetch_task)):
        return False
    return (state.speed == 1.0 and state.pitch == 0 and state.effect == "off"
            and state.volume == state.default_volume)


def drop_if_idle(guild_id: int) -> bool:
    """Unregister a guild whose state is empty. Returns True if dropped.

    Nearly every command runs hydrate_state, which registers a GuildState even
    for a guild that never plays anything — and the only unregister path
    (cleanup_guild_state) assumes the bot was in a VC. Without this, browsing
    commands alone grow guild_states without bound.
    """
    state = guild_states.get(guild_id)
    if state is None or not is_idle(state):
        return False
    guild_states.pop(guild_id, None)
    return True


def move_queue_item(queue: List[Any], from_pos: int, to_pos: int) -> bool:
    """Move a queue entry from 1-based from_pos to to_pos, in place.

    Returns False (no change) for an empty/single queue, out-of-range
    positions, or a no-op move.
    """
    n = len(queue)
    if n < 2 or not (1 <= from_pos <= n) or not (1 <= to_pos <= n) or from_pos == to_pos:
        return False
    queue.insert(to_pos - 1, queue.pop(from_pos - 1))
    return True
