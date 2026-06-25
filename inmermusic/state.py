"""Per-guild playback state and the in-memory registry."""
import asyncio
from typing import Any, Dict, List, Optional

import discord


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
        self.reapply_task: Optional[asyncio.Task] = None    # debounced source-swap timer


guild_states: Dict[int, GuildState] = {}


def get_state(guild_id: int) -> GuildState:
    if guild_id not in guild_states:
        guild_states[guild_id] = GuildState()
    return guild_states[guild_id]


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
