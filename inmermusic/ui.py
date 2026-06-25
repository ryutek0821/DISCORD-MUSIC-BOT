"""Now-playing / queue embeds and the interactive MusicControls view.

MusicControls button handlers import `playback` lazily (inside the methods) so
this module stays importable without pulling in playback at load time, which
would otherwise create a ui <-> playback import cycle.
"""
import random
from typing import Any, Dict, Optional

import discord

from .config import (EFFECT_LABELS, EFFECT_PRESETS, PITCH_MAX, PITCH_MIN,
                     SPEED_MAX, SPEED_MIN, SPEED_STEP)
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


class MusicControls(discord.ui.View):
    """Interactive control buttons attached to the now-playing embed."""

    def __init__(self):
        # Persistent view (timeout=None), registered once via bot.add_view in
        # on_ready so the buttons keep working after a restart. Handlers resolve
        # the guild from the interaction, so no per-guild state is stored here.
        super().__init__(timeout=None)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause_resume")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
        elif vc and vc.is_paused():
            vc.resume()

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
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        state = guild_states.get(interaction.guild.id)
        if state:
            playback.cancel_np_updater(state)
            playback.cancel_reapply(state)
        if interaction.guild.id in guild_states:
            del guild_states[interaction.guild.id]

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認。状態は再生パネルに表示
        from . import playback
        state = get_state(interaction.guild.id)
        order = {"off": "song", "song": "queue", "queue": "off"}
        state.loop_mode = order.get(state.loop_mode, "off")
        await playback.refresh_now_playing(interaction.guild.id)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, custom_id="music:shuffle")
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()  # 無言で承認（ポップアップなし）
        state = get_state(interaction.guild.id)
        if len(state.queue) >= 2:
            random.shuffle(state.queue)

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
        options=[
            discord.SelectOption(label="オフ（通常）", value="off", emoji="🎚️"),
            discord.SelectOption(label="ナイトコア", value="nightcore", emoji="⚡", description="1.25x / +3半音"),
            discord.SelectOption(label="ベイパーウェイブ", value="vaporwave", emoji="🌊", description="0.85x / -3半音"),
            discord.SelectOption(label="低音ブースト", value="bassboost", emoji="🔊"),
            discord.SelectOption(label="8Dオーディオ", value="8d", emoji="🎧"),
            discord.SelectOption(label="Lo-Fi", value="lofi", emoji="📼", description="0.9x"),
            discord.SelectOption(label="エコー", value="echo", emoji="📢"),
            discord.SelectOption(label="リバーブ", value="reverb", emoji="🏛️"),
            discord.SelectOption(label="トレモロ", value="tremolo", emoji="📳"),
            discord.SelectOption(label="ボーカルカット", value="karaoke", emoji="🎤"),
            discord.SelectOption(label="高音ブースト", value="trebleboost", emoji="🔔"),
        ],
    )
    async def preset_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._apply_preset(interaction, select.values[0])
