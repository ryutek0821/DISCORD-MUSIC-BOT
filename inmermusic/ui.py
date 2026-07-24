"""Now-playing / queue embeds and the interactive MusicControls view.

MusicControls button handlers import `playback` lazily (inside the methods) so
this module stays importable without pulling in playback at load time, which
would otherwise create a ui <-> playback import cycle.
"""
import random
from typing import Any, Dict, Optional

import discord

from .config import (EFFECT_EMOJI, EFFECT_LABELS, EFFECT_PRESETS, PITCH_MAX,
                     PITCH_MIN, SPEED_MAX, SPEED_MIN, SPEED_STEP)
from .state import GuildState, get_state, guild_states
from .util import fmt_duration


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
    parts = []
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
    title = "再生中"
    if state is not None and state.loop_mode != "off":
        loop_labels = {"song": "🔁 1曲リピート", "queue": "🔁 全体リピート"}
        title += f"  {loop_labels.get(state.loop_mode, '')}"
    embed = discord.Embed(
        title=title,
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


def create_queue_embed(state: GuildState, *, page: int = 0, page_size: int = 10,
                       current_remaining: float = 0.0) -> discord.Embed:
    """Render one queue page with an estimated start time for each track."""
    total = len(state.queue)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    start = page * page_size
    end = min(total, start + page_size)
    eta = max(0.0, current_remaining)
    for song in state.queue[:start]:
        eta += (song.get("duration") or 0) / max(0.01, state.speed)
    lines = []
    for index, song in enumerate(state.queue[start:end], start=start + 1):
        eta_label = f"・約{fmt_duration(eta)}後" if eta > 0 else ""
        requester = song.get("requester", "不明")
        lines.append(
            f"{index}. **[{song['title']}]({song['url']})** "
            f"(by {requester}{eta_label})"
        )
        eta += (song.get("duration") or 0) / max(0.01, state.speed)
    embed = discord.Embed(
        title=f"キュー（{total}曲）",
        description="\n".join(lines) or "キューは空です。",
        color=0x00ff00,
    )
    total_duration = sum((song.get("duration") or 0) for song in state.queue)
    footer = f"ページ {page + 1}/{page_count}"
    if total_duration:
        footer += f"・合計 {fmt_duration(total_duration / max(0.01, state.speed))}"
    embed.set_footer(text=footer)
    return embed


def _preset_description(preset: Dict[str, Any]) -> Optional[str]:
    """Short '1.25x / +3半音' hint from a preset's speed/pitch, or None."""
    parts = []
    if abs(preset["speed"] - 1.0) > 1e-6:
        parts.append(f"{preset['speed']:.2f}x")
    if preset["pitch"]:
        parts.append(f"{preset['pitch']:+d}半音")
    return " / ".join(parts) or None


# Built once from config so adding an effect in config.py also updates the
# dropdown (Discord allows at most 25 options).
_PRESET_OPTIONS = [
    discord.SelectOption(
        label=EFFECT_LABELS[key],
        value=key,
        emoji=EFFECT_EMOJI.get(key),
        description=_preset_description(preset),
    )
    for key, preset in EFFECT_PRESETS.items()
]


class MusicControls(discord.ui.View):
    """Interactive control buttons attached to the now-playing embed."""

    def __init__(self):
        # Persistent view (timeout=None), registered once via bot.add_view in
        # on_ready so the buttons keep working after a restart. Handlers resolve
        # the guild from the interaction, so no per-guild state is stored here.
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "この操作はサーバー内で使用してください。", ephemeral=True
            )
            return False
        vc = interaction.guild.voice_client
        if not vc or interaction.guild.id not in guild_states:
            await interaction.response.send_message(
                "現在再生中の曲はありません。", ephemeral=True
            )
            return False
        state = guild_states[interaction.guild.id]
        message = getattr(interaction, "message", None)
        current_message = state.np_message
        if (current_message is None or message is None
                or getattr(message, "id", None) != getattr(current_message, "id", None)):
            await interaction.response.send_message(
                "この操作パネルは古くなっています。最新の再生パネルを使用してください。",
                ephemeral=True,
            )
            return False
        user_voice = getattr(interaction.user, "voice", None)
        if vc and vc.channel and (not user_voice or user_voice.channel != vc.channel):
            await interaction.response.send_message(
                "BOTと同じVCに参加してから操作してください。", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        from . import playback
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            playback.mark_paused(get_state(interaction.guild.id), vc)
            vc.pause()
        elif vc and vc.is_paused():
            vc.resume()
            playback.mark_resumed(get_state(interaction.guild.id))

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = get_state(interaction.guild.id)
            state.skip_flag = True
            vc.stop()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        from . import playback
        playback.cancel_idle_task(interaction.guild.id)
        state = guild_states.get(interaction.guild.id)
        if state:
            await playback.retire_now_playing(state)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        playback.cleanup_guild_state(interaction.guild.id)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認。状態は再生パネルに表示
        from . import playback
        state = get_state(interaction.guild.id)
        order = {"off": "song", "song": "queue", "queue": "off"}
        state.loop_mode = order.get(state.loop_mode, "off")
        from . import persistence
        persistence.update_settings(interaction.guild.id, loop_mode=state.loop_mode)
        await playback.refresh_now_playing(interaction.guild.id)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        state = get_state(interaction.guild.id)
        if len(state.queue) >= 2:
            random.shuffle(state.queue)
            from . import playback
            playback.persist_queue(state)
            playback.start_prefetch(interaction.guild.id)

    async def _apply_speed_pitch(self, interaction: discord.Interaction, *,
                                 speed: Optional[float] = None,
                                 pitch: Optional[int] = None,
                                 effect: Optional[str] = None):
        await interaction.response.defer()  # 無言で承認。変更は再生パネルに反映
        from . import playback
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return
        if state.is_playing_sound:
            return
        if speed is not None:
            state.speed = speed
        if pitch is not None:
            state.pitch = pitch
        if effect is not None:
            state.effect = effect
        playback.schedule_reapply(interaction.guild.id)
        await playback.refresh_now_playing(interaction.guild.id)

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
        await interaction.response.defer()  # 無言で承認。変更は再生パネルに反映
        from . import playback
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return
        if state.is_playing_sound:
            return
        preset = EFFECT_PRESETS[preset_key]
        state.speed = preset["speed"]
        state.pitch = preset["pitch"]
        state.effect = preset["effect"]
        playback.schedule_reapply(interaction.guild.id)
        await playback.refresh_now_playing(interaction.guild.id)

    @discord.ui.select(
        placeholder="🎛️ エフェクトプリセットを選択…",
        row=2,
        min_values=1,
        max_values=1,
        custom_id="music:preset_select",
        options=_PRESET_OPTIONS,
    )
    async def preset_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._apply_preset(interaction, select.values[0])


class QueuePaginationView(discord.ui.View):
    """Requester-scoped, live queue pagination."""

    def __init__(self, guild_id: int, requester_id: int, page_count: int):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.page = 0
        self.page_count = max(1, page_count)
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "このページ送りはコマンド実行者のみ操作できます。", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def _render(self, interaction: discord.Interaction) -> None:
        from .audio import current_elapsed
        state = get_state(self.guild_id)
        self.page_count = max(1, (len(state.queue) + 9) // 10)
        self.page = max(0, min(self.page, self.page_count - 1))
        vc = interaction.guild.voice_client if interaction.guild else None
        remaining = 0.0
        if vc and state.current_song and (vc.is_playing() or vc.is_paused()):
            duration = state.current_song.get("duration") or 0
            if duration:
                remaining = max(0.0, duration - current_elapsed(vc, state)) / max(
                    0.01, state.speed)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=create_queue_embed(
                state, page=self.page, current_remaining=remaining),
            view=self,
        )

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self._render(interaction)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._render(interaction)
