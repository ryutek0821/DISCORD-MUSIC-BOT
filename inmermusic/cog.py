"""All slash commands and the message/voice listeners, as a single cog."""
import asyncio
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from . import cookies, persistence
from .audio import (cleanup_download, current_elapsed, extract_audio_url,
                    extract_playlist, is_playlist_url, search_candidates, swap_source_at,
                    validate_query)
from .config import (EFFECT_LABELS, EFFECT_PRESETS, MAX_PLAYLIST_NAME_LEN,
                     MAX_PLAYLIST_SIZE, MAX_PLAYLISTS_PER_GUILD, MAX_QUEUE_SIZE,
                     list_sound_names, logger,
                     resolve_sound)
from .playback import (cancel_idle_task, cancel_prefetch, cancel_reapply,
                       cleanup_guild_state, mark_paused, mark_resumed,
                       persist_queue, play_next, play_sound_effect,
                       refresh_now_playing, resolve_text_channel,
                       retire_now_playing,
                       schedule_reapply, start_np_updater, start_prefetch)
from .state import get_state, guild_states, hydrate_state, move_queue_item
from .ui import (MusicControls, QueuePaginationView, create_now_playing_embed,
                 create_queue_embed, create_queued_embed)
from .util import fmt_duration, friendly_extract_error, parse_time


# /preset choices generated from config so adding an effect there is the only
# edit needed (Discord allows at most 25 choices).
_PRESET_CHOICES = [
    app_commands.Choice(name=EFFECT_LABELS[k], value=k) for k in EFFECT_PRESETS
]


def _drop_abandoned_state(guild_id: int, request_state, created: bool) -> None:
    """Drop a GuildState this /play call just created, if extraction failed
    before anything else touched it.

    "queue is empty" alone does NOT mean "nothing happened" — a song may
    already be playing (popped from the queue into current_song). Only ever
    remove a state this very call created, and only when it's still
    completely untouched (no queue, no current song, no voice client).
    """
    if not created:
        return
    if (guild_states.get(guild_id) is request_state and not request_state.queue
            and request_state.current_song is None
            and request_state.voice_client is None):
        guild_states.pop(guild_id, None)


class SearchResultView(discord.ui.View):
    """Short-lived, requester-only selection of YouTube search candidates."""

    def __init__(self, cog: "MusicCog", candidates, requester_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.candidates = candidates
        self.requester_id = requester_id
        self.message = None
        options = []
        for index, song in enumerate(candidates):
            details = []
            if song.get("uploader"):
                details.append(str(song["uploader"]))
            if song.get("duration"):
                details.append(fmt_duration(song["duration"]))
            description = "・".join(details)[:100] or None
            options.append(discord.SelectOption(
                label=str(song.get("title") or "Unknown")[:100],
                value=str(index),
                description=description,
            ))
        self.selector = discord.ui.Select(
            placeholder="再生する曲を選択", options=options,
            min_values=1, max_values=1,
        )
        self.selector.callback = self._selected
        self.add_item(self.selector)

    async def on_timeout(self) -> None:
        self.selector.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "検索した本人のみ選択できます。", ephemeral=True)
            return False
        return True

    async def _selected(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        self.selector.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        song = dict(self.candidates[int(self.selector.values[0])])
        await self.cog._enqueue_songs(interaction, [song])
        self.stop()


class MusicCog(commands.Cog):
    """NicoNico/YouTube music playback commands and triggers."""

    playlist_group = app_commands.Group(
        name="playlist", description="プレイリストの追加・保存・管理")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Reject DMs and prevent users from hijacking another VC's player."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "このコマンドはサーバー内で使用してください。", ephemeral=True
            )
            return False
        command = (
            getattr(interaction.command, "qualified_name", None)
            or getattr(interaction.command, "name", "")
        )
        if command not in {
            "help", "history", "favorites", "refresh", "settings",
            "playlist list", "playlist delete",
        }:
            hydrate_state(interaction.guild.id)
        if command in {"refresh", "settings"}:
            allowed = getattr(interaction.user.guild_permissions, "manage_guild", False)
            if not allowed:
                await interaction.response.send_message(
                    "この操作にはサーバー管理権限が必要です。", ephemeral=True
                )
            return allowed
        if command in {
            "help", "queue", "nowplaying", "history", "favorites",
            "playlist list", "playlist delete",
        }:
            return True
        vc = interaction.guild.voice_client
        user_voice = getattr(interaction.user, "voice", None)
        if vc and vc.channel and (not user_voice or user_voice.channel != vc.channel):
            await interaction.response.send_message(
                "BOTと同じVCに参加してから操作してください。", ephemeral=True
            )
            return False
        return True

    @app_commands.command(name="play", description="Play a song from NicoNico or YouTube")
    @app_commands.describe(query="NicoNico URL, YouTube URL, or search keyword")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return

        try:
            validate_query(query)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.defer()
        if is_playlist_url(query):
            await self._enqueue_playlist_url(
                interaction, query, MAX_PLAYLIST_SIZE)
            return
        loop = asyncio.get_running_loop()
        try:
            if "://" not in query:
                candidates = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, search_candidates, query, interaction.guild.id, 5),
                    timeout=60,
                )
                if not candidates:
                    await interaction.followup.send("検索結果が見つかりませんでした。")
                    return
                view = SearchResultView(self, candidates, interaction.user.id)
                view.message = await interaction.followup.send(
                    "再生する曲を選択してください。", view=view, wait=True)
                return
            song = await asyncio.wait_for(
                loop.run_in_executor(
                    None, extract_audio_url, query, interaction.guild.id),
                timeout=60,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("曲の取得がタイムアウトしました。")
            return
        except Exception as e:
            await interaction.followup.send(friendly_extract_error(str(e)))
            return
        await self._enqueue_songs(interaction, [song])

    async def _enqueue_playlist_url(
        self, interaction: discord.Interaction, url: str, limit: int,
    ) -> None:
        try:
            songs = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, extract_playlist, url, interaction.guild.id, limit),
                timeout=60,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "プレイリスト取得がタイムアウトしました。")
            return
        except Exception as e:
            await interaction.followup.send(friendly_extract_error(str(e)))
            return
        if not songs:
            await interaction.followup.send(
                "追加できる曲が見つかりませんでした。")
            return
        await self._enqueue_songs(interaction, songs, deduplicate=True)

    async def _enqueue_songs(self, interaction: discord.Interaction, songs,
                             *, deduplicate: bool = False) -> None:
        """Connect once and enqueue one or many already-extracted songs."""
        if not interaction.user.voice:
            await interaction.followup.send("VCに参加してください。")
            return
        guild_id = interaction.guild.id
        state = hydrate_state(guild_id)
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        connected_here = False
        if not vc:
            try:
                vc = await channel.connect(timeout=15)
                connected_here = True
            except Exception as e:
                await interaction.followup.send(f"VC接続失敗: {str(e)}")
                return
        elif vc.channel != channel:
            try:
                await vc.move_to(channel)
            except Exception as e:
                await interaction.followup.send(f"チャンネル移動失敗: {str(e)}")
                return
        if guild_states.get(guild_id) is not state:
            if connected_here:
                try:
                    await vc.disconnect()
                except Exception as e:
                    logger.warning(f"Failed to disconnect abandoned VC: {e}")
            await interaction.followup.send("再生リクエストはキャンセルされました。")
            return
        state.voice_client = vc
        cancel_idle_task(guild_id)

        async with state.lock:
            existing_urls = {
                song.get("url") for song in
                (([state.current_song] if state.current_song else []) + state.queue)
            }
            prepared = []
            for raw_song in songs:
                song = dict(raw_song)
                if deduplicate and song.get("url") in existing_urls:
                    continue
                song["text_channel_id"] = interaction.channel.id
                song["requester"] = interaction.user.display_name
                song["requester_id"] = interaction.user.id
                prepared.append(song)
                existing_urls.add(song.get("url"))

            available = max(0, MAX_QUEUE_SIZE - len(state.queue))
            prepared = prepared[:available]
            if prepared:
                state.queue.extend(prepared)
                persist_queue(state)
        if not prepared:
            await interaction.followup.send(
                "追加できる曲がありません（上限または重複）。", ephemeral=True)
            return

        was_busy = vc.is_playing() or vc.is_paused() or state.dispatching

        if was_busy:
            if len(prepared) == 1:
                embed = create_queued_embed(prepared[0], len(state.queue))
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    f"📚 **{len(prepared)}曲**をキューへ追加しました。")
            start_prefetch(guild_id)
        else:
            await play_next(guild_id, announce=False)
            if guild_states.get(guild_id) is not state:
                await interaction.followup.send("再生は停止されました。")
                return
            started = state.current_song
            if started:
                embed = create_now_playing_embed(started, elapsed=0.0, state=state)
                if len(prepared) > 1 or started not in prepared:
                    embed.set_footer(text=f"新たに {len(prepared)} 曲を追加")
                state.np_message = await interaction.followup.send(
                    embed=embed, view=MusicControls(), wait=True)
                start_np_updater(guild_id)
            elif vc.is_playing() or vc.is_paused():
                await interaction.followup.send(
                    f"📚 **{len(prepared)}曲**をキューへ追加しました。")
            else:
                await interaction.followup.send("⚠️ 再生できませんでした。")

    @playlist_group.command(name="add", description="YouTube/NicoNicoプレイリストを追加")
    @app_commands.describe(url="プレイリストURL", limit="追加する最大曲数")
    async def playlist_add(
        self, interaction: discord.Interaction, url: str,
        limit: app_commands.Range[int, 1, MAX_PLAYLIST_SIZE] = MAX_PLAYLIST_SIZE,
    ):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return
        try:
            validate_query(url)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.defer()
        await self._enqueue_playlist_url(interaction, url, limit)

    @playlist_group.command(name="save", description="現在の曲とキューを名前付きで保存")
    @app_commands.describe(name="保存する名前")
    async def playlist_save(self, interaction: discord.Interaction, name: str):
        stripped = name.strip()
        if not stripped:
            await interaction.response.send_message(
                "名前を入力してください。", ephemeral=True)
            return
        if len(stripped) > MAX_PLAYLIST_NAME_LEN or "\n" in stripped:
            await interaction.response.send_message(
                f"名前は改行なし・{MAX_PLAYLIST_NAME_LEN}文字以内で指定してください。",
                ephemeral=True,
            )
            return
        state = get_state(interaction.guild.id)
        songs = (
            ([state.current_song] if state.current_song else [])
            + list(state.queue)
        )
        if not songs:
            await interaction.response.send_message(
                "保存できる曲がありません。", ephemeral=True)
            return
        rows = persistence.list_named_playlists(interaction.guild.id)
        overwriting = any(
            row["name"].casefold() == stripped.casefold() for row in rows)
        if not overwriting and len(rows) >= MAX_PLAYLISTS_PER_GUILD:
            await interaction.response.send_message(
                f"保存できるプレイリストは{MAX_PLAYLISTS_PER_GUILD}個までです。",
                ephemeral=True,
            )
            return
        saved = persistence.save_named_playlist(
            interaction.guild.id, stripped, songs, interaction.user.id)
        if not saved:
            await interaction.response.send_message(
                "プレイリストの保存に失敗しました。", ephemeral=True)
            return
        suffix = "（既存データを上書き）" if overwriting else ""
        await interaction.response.send_message(
            f"💾 **{stripped}** を保存しました（{len(songs)}曲）。{suffix}")

    @playlist_group.command(name="load", description="保存済みプレイリストをキューに追加")
    @app_commands.describe(name="読み込むプレイリスト名")
    async def playlist_load(self, interaction: discord.Interaction, name: str):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return
        songs = persistence.load_named_playlist(interaction.guild.id, name)
        if not songs:
            await interaction.response.send_message(
                "そのプレイリストは見つかりません。", ephemeral=True)
            return
        await interaction.response.defer()
        await self._enqueue_songs(interaction, songs)

    @playlist_group.command(name="list", description="保存済みプレイリストを表示")
    async def playlist_list(self, interaction: discord.Interaction):
        rows = persistence.list_named_playlists(interaction.guild.id)
        if not rows:
            await interaction.response.send_message(
                "保存されたプレイリストはありません。", ephemeral=True)
            return
        embed = discord.Embed(title="📃 保存されたプレイリスト", color=0x00ff00)
        for row in rows:
            embed.add_field(
                name=row["name"],
                value=f"{row['song_count']}曲・更新 <t:{row['updated_at']}:f>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @playlist_group.command(name="delete", description="保存済みプレイリストを削除")
    @app_commands.describe(name="削除するプレイリスト名")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        if persistence.delete_named_playlist(interaction.guild.id, name):
            await interaction.response.send_message(
                f"🗑️ **{name}** を削除しました。", ephemeral=True)
        else:
            await interaction.response.send_message(
                "そのプレイリストは見つかりません。", ephemeral=True)

    async def _playlist_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ):
        if not interaction.guild:
            return []
        needle = current.casefold()
        return [
            app_commands.Choice(name=row["name"], value=row["name"])
            for row in persistence.list_named_playlists(interaction.guild.id)
            if needle in row["name"].casefold()
        ][:25]

    @playlist_load.autocomplete("name")
    async def playlist_load_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_delete.autocomplete("name")
    async def playlist_delete_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ):
        return await self._playlist_autocomplete(interaction, current)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生していません。")
            return
        state = get_state(interaction.guild.id)
        state.skip_flag = True
        vc.stop()
        await interaction.response.send_message("スキップしました！")

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if not state.queue:
            await interaction.response.send_message("キューは空です。")
            return
        remaining = 0.0
        vc = interaction.guild.voice_client
        if vc and state.current_song and (vc.is_playing() or vc.is_paused()):
            duration = state.current_song.get("duration") or 0
            if duration:
                remaining = max(0.0, duration - current_elapsed(vc, state)) / max(
                    0.01, state.speed)
        embed = create_queue_embed(state, current_remaining=remaining)
        page_count = max(1, (len(state.queue) + 9) // 10)
        view = (
            QueuePaginationView(interaction.guild.id, interaction.user.id, page_count)
            if page_count > 1 else None
        )
        await interaction.response.send_message(embed=embed, view=view)
        if view is not None:
            try:
                view.message = await interaction.original_response()
            except Exception as e:
                logger.debug(f"Failed to retain queue pager message: {e}")

    @app_commands.command(name="loop", description="Set repeat mode (off / song / queue)")
    @app_commands.describe(mode="リピートモード")
    @app_commands.choices(mode=[
        app_commands.Choice(name="オフ", value="off"),
        app_commands.Choice(name="1曲リピート", value="song"),
        app_commands.Choice(name="キュー全体リピート", value="queue"),
    ])
    async def loop_cmd(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        state = get_state(interaction.guild.id)
        state.loop_mode = mode.value
        persistence.update_settings(interaction.guild.id, loop_mode=state.loop_mode)
        labels = {"off": "オフ", "song": "1曲リピート", "queue": "キュー全体リピート"}
        await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(f"🔁 リピート: **{labels[state.loop_mode]}**")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    async def shuffle_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if len(state.queue) < 2:
            await interaction.response.send_message("シャッフルする曲が足りません。")
            return
        random.shuffle(state.queue)
        persist_queue(state)
        start_prefetch(interaction.guild.id)
        await interaction.response.send_message(f"🔀 キュー（{len(state.queue)}曲）をシャッフルしました。")

    @app_commands.command(name="speed", description="Set playback speed (0.5-2.0x, pitch preserved)")
    @app_commands.describe(rate="再生速度 (0.5〜2.0)")
    async def speed_cmd(self, interaction: discord.Interaction, rate: app_commands.Range[float, 0.5, 2.0]):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        state.speed = round(rate, 2)
        if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
            schedule_reapply(interaction.guild.id)
            await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(f"🎚️ 速度を **{state.speed:.2f}x** にしました。")

    @app_commands.command(name="pitch", description="Set pitch shift in semitones (-12 to +12)")
    @app_commands.describe(semitones="ピッチ (-12〜+12半音)")
    async def pitch_cmd(self, interaction: discord.Interaction, semitones: app_commands.Range[int, -12, 12]):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        state.pitch = semitones
        if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
            schedule_reapply(interaction.guild.id)
            await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(f"🎚️ ピッチを **{state.pitch:+d}半音** にしました。")

    @app_commands.command(name="seek", description="Jump to a position in the current song")
    @app_commands.describe(position="再生位置（秒 または mm:ss、例: 90 / 1:30）")
    async def seek_cmd(self, interaction: discord.Interaction, position: str):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生していません。")
            return
        if state.is_playing_sound:
            await interaction.response.send_message("効果音の再生中は変更できません。")
            return
        secs = parse_time(position)
        if secs is None or secs < 0:
            await interaction.response.send_message(
                "時間の形式が不正です（例: `90` または `1:30`）。"
            )
            return
        duration = (state.current_song or {}).get("duration") or 0
        if duration and secs >= duration:
            await interaction.response.send_message(
                f"曲の長さ（{fmt_duration(duration)}）以内で指定してください。"
            )
            return
        cancel_reapply(state)  # an explicit jump supersedes a pending effect swap
        swap_source_at(vc, state, secs)
        await interaction.response.send_message(f"⏩ **{fmt_duration(secs)}** へシークしました。")

    @app_commands.command(name="volume", description="Set playback volume (0-200%)")
    @app_commands.describe(level="音量 (0〜200)")
    async def volume_cmd(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 200]):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        state.volume = level
        if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
            schedule_reapply(interaction.guild.id)
            await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(f"🔊 音量を **{level}%** にしました。")

    @app_commands.command(name="preset", description="Apply an audio effect preset")
    @app_commands.describe(name="エフェクトプリセット")
    @app_commands.choices(name=_PRESET_CHOICES)
    async def preset_cmd(self, interaction: discord.Interaction, name: app_commands.Choice[str]):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        preset = EFFECT_PRESETS[name.value]
        state.speed = preset["speed"]
        state.pitch = preset["pitch"]
        state.effect = preset["effect"]
        if vc and (vc.is_playing() or vc.is_paused()) and not state.is_playing_sound:
            schedule_reapply(interaction.guild.id)
            await refresh_now_playing(interaction.guild.id)
        await interaction.response.send_message(f"🎛️ プリセット **{name.name}** を適用しました。")

    @app_commands.command(name="remove", description="Remove a song from the queue by position")
    @app_commands.describe(position="削除するキューの番号（1から）")
    async def remove_cmd(self, interaction: discord.Interaction, position: int):
        state = get_state(interaction.guild.id)
        if not state.queue:
            await interaction.response.send_message("キューは空です。")
            return
        if position < 1 or position > len(state.queue):
            await interaction.response.send_message(
                f"1〜{len(state.queue)} の範囲で指定してください。"
            )
            return
        removed = state.queue.pop(position - 1)
        cleanup_download(removed.get("local_file"))
        cancel_prefetch(state)
        persist_queue(state)
        start_prefetch(interaction.guild.id)
        await interaction.response.send_message(f"🗑️ 削除しました: **{removed['title']}**")

    @app_commands.command(name="move", description="Reorder a song in the queue")
    @app_commands.describe(from_pos="移動元の番号", to_pos="移動先の番号")
    async def move_cmd(self, interaction: discord.Interaction, from_pos: int, to_pos: int):
        state = get_state(interaction.guild.id)
        n = len(state.queue)
        if n < 2:
            await interaction.response.send_message("並べ替える曲がありません。")
            return
        if not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
            await interaction.response.send_message(f"1〜{n} の範囲で指定してください。")
            return
        if from_pos == to_pos:
            await interaction.response.send_message("移動元と移動先が同じです。")
            return
        title = state.queue[from_pos - 1]["title"]
        move_queue_item(state.queue, from_pos, to_pos)
        cancel_prefetch(state)
        persist_queue(state)
        start_prefetch(interaction.guild.id)
        await interaction.response.send_message(
            f"↕️ **{title}** を #{from_pos} → #{to_pos} に移動しました。"
        )

    @app_commands.command(name="clear", description="Clear the queue without disconnecting")
    async def clear_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        count = len(state.queue)
        for song in state.queue:
            cleanup_download(song.get("local_file"))
        cancel_prefetch(state)
        state.queue.clear()
        persist_queue(state)
        await interaction.response.send_message(
            f"🧹 キューをクリアしました（{count}曲）。再生中の曲は継続します。"
        )

    @app_commands.command(name="history", description="Show recently played tracks")
    async def history_cmd(self, interaction: discord.Interaction):
        songs = persistence.load_history(interaction.guild.id, 20)
        if not songs:
            await interaction.response.send_message("再生履歴はありません。", ephemeral=True)
            return
        lines = [
            f"{index}. **[{song['title']}]({song['url']})** "
            f"(<t:{song['played_at']}:R>)"
            for index, song in enumerate(songs, start=1)
        ]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="最近の再生履歴", description="\n".join(lines),
                color=0x00ff00,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="previous", description="Play the previous song again")
    async def previous_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not state.current_song or not (
                vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生中の曲はありません。")
            return
        if len(state.queue) >= MAX_QUEUE_SIZE:
            await interaction.response.send_message(
                f"キューが上限（{MAX_QUEUE_SIZE}曲）に達しています。",
                ephemeral=True,
            )
            return
        song = persistence.pop_history(interaction.guild.id)
        if song is None:
            await interaction.response.send_message("以前の再生履歴はありません。")
            return
        state.queue.insert(0, song)
        cancel_prefetch(state)
        persist_queue(state)
        start_prefetch(interaction.guild.id)
        await interaction.response.send_message(
            f"⏮️ **{song['title']}** に戻ります。")
        state.skip_flag = True
        vc.stop()

    @app_commands.command(name="replay", description="Replay the current song from the beginning")
    async def replay_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("再生していません。")
            return
        if state.is_playing_sound:
            await interaction.response.send_message(
                "効果音の再生中は変更できません。")
            return
        cancel_reapply(state)
        swap_source_at(vc, state, 0.0)
        await interaction.response.send_message("⏪ 最初から再生し直します。")

    @app_commands.command(name="historyplay", description="Add a track from recent history")
    @app_commands.describe(position="履歴の番号（1〜20）")
    async def historyplay_cmd(self, interaction: discord.Interaction, position: int):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return
        songs = persistence.load_history(interaction.guild.id, 20)
        if not 1 <= position <= len(songs):
            await interaction.response.send_message(
                f"1〜{len(songs)} の範囲で指定してください。" if songs
                else "再生履歴はありません。",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self._enqueue_songs(interaction, [songs[position - 1]])

    @app_commands.command(name="favorite", description="Save the current track to favorites")
    async def favorite_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if not state.current_song:
            await interaction.response.send_message("再生中の曲はありません。", ephemeral=True)
            return
        if persistence.add_favorite(
                interaction.guild.id, interaction.user.id, state.current_song):
            await interaction.response.send_message(
                f"⭐ お気に入りへ追加しました: **{state.current_song['title']}**",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "お気に入りの保存に失敗しました。", ephemeral=True)

    @app_commands.command(name="favorites", description="Show your favorite tracks")
    async def favorites_cmd(self, interaction: discord.Interaction):
        songs = persistence.load_favorites(
            interaction.guild.id, interaction.user.id, 25)
        if not songs:
            await interaction.response.send_message(
                "お気に入りはありません。", ephemeral=True)
            return
        lines = [
            f"{index}. **[{song['title']}]({song['url']})**"
            for index, song in enumerate(songs, start=1)
        ]
        await interaction.response.send_message(
            embed=discord.Embed(
                title="お気に入り", description="\n".join(lines),
                color=0xffc107,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="playfavorite", description="Play one of your favorites")
    @app_commands.describe(position="お気に入りの番号")
    async def playfavorite_cmd(self, interaction: discord.Interaction, position: int):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return
        songs = persistence.load_favorites(
            interaction.guild.id, interaction.user.id, 25)
        if not 1 <= position <= len(songs):
            await interaction.response.send_message(
                f"1〜{len(songs)} の範囲で指定してください。" if songs
                else "お気に入りはありません。",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self._enqueue_songs(interaction, [songs[position - 1]])

    @app_commands.command(name="unfavorite", description="Remove one of your favorites")
    @app_commands.describe(position="お気に入りの番号")
    async def unfavorite_cmd(self, interaction: discord.Interaction, position: int):
        removed = persistence.remove_favorite(
            interaction.guild.id, interaction.user.id, position)
        if removed:
            await interaction.response.send_message(
                f"お気に入りから削除しました: **{removed['title']}**", ephemeral=True)
        else:
            await interaction.response.send_message(
                "指定されたお気に入りはありません。", ephemeral=True)

    @app_commands.command(name="settings", description="Show or update guild playback defaults")
    @app_commands.describe(
        default_volume="新しいデフォルト音量（省略時は変更なし）",
        idle_timeout="アイドル切断までの秒数（省略時は変更なし）",
    )
    async def settings_cmd(
        self, interaction: discord.Interaction,
        default_volume: Optional[app_commands.Range[int, 0, 200]] = None,
        idle_timeout: Optional[app_commands.Range[int, 30, 3600]] = None,
    ):
        settings = persistence.update_settings(
            interaction.guild.id,
            default_volume=default_volume,
            idle_timeout=idle_timeout,
        ) if default_volume is not None or idle_timeout is not None \
            else persistence.get_settings(interaction.guild.id)
        state = guild_states.get(interaction.guild.id)
        if state is not None:
            state.idle_timeout = settings["idle_timeout"]
            if default_volume is not None and not (
                    state.voice_client and (
                        state.voice_client.is_playing() or state.voice_client.is_paused())):
                state.volume = settings["default_volume"]
        await interaction.response.send_message(
            "⚙️ サーバー設定\n"
            f"デフォルト音量: **{settings['default_volume']}%**\n"
            f"アイドル切断: **{settings['idle_timeout']}秒**\n"
            f"デフォルトリピート: **{settings['loop_mode']}**",
            ephemeral=True,
        )

    @app_commands.command(name="join", description="Join your voice channel")
    async def join_cmd(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("先にVCに参加してください。")
            return
        await interaction.response.defer()
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        try:
            if vc:
                await vc.move_to(channel)
            else:
                vc = await channel.connect(timeout=15)
            state.voice_client = vc
        except Exception as e:
            await interaction.followup.send(f"VC接続失敗: {str(e)}")
            return
        restored = len(state.queue)
        if restored and not (vc.is_playing() or vc.is_paused()):
            await play_next(interaction.guild.id, announce=True)
        suffix = f"（保存キュー {restored}曲を復元）" if restored else ""
        await interaction.followup.send(f"🔊 接続しました: **{channel.name}**{suffix}")

    @app_commands.command(name="leave", description="Disconnect from the voice channel")
    async def leave_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cancel_idle_task(interaction.guild.id)
        state = guild_states.get(interaction.guild.id)
        if state:
            await retire_now_playing(state)
        vc = interaction.guild.voice_client
        if not vc:
            cleanup_guild_state(interaction.guild.id)
            await interaction.followup.send("VCに接続していません。")
            return
        vc.stop()
        await vc.disconnect()
        cleanup_guild_state(interaction.guild.id)
        await interaction.followup.send("👋 退出しました。")

    @app_commands.command(name="help", description="Show available commands")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="INMERMUSIC BOT コマンド一覧", color=0x00ff00)
        embed.add_field(name="/play <URL/キーワード>", value="ニコニコ/YouTube/検索キーワードで再生", inline=False)
        embed.add_field(name="/skip", value="現在の曲をスキップ", inline=True)
        embed.add_field(name="/pause・/resume", value="一時停止・再開", inline=True)
        embed.add_field(name="/stop", value="停止してキュー削除・退出", inline=True)
        embed.add_field(name="/queue", value="キューを表示", inline=True)
        embed.add_field(
            name="/playlist add・save・load・list・delete",
            value="プレイリストの一括追加・保存・管理", inline=False)
        embed.add_field(name="/nowplaying", value="再生中の曲を表示", inline=True)
        embed.add_field(name="/loop <mode>", value="リピート (off/song/queue)", inline=True)
        embed.add_field(name="/shuffle", value="キューをシャッフル", inline=True)
        embed.add_field(name="/speed <0.5-2.0>", value="再生速度（ピッチ維持）", inline=True)
        embed.add_field(name="/pitch <-12〜12>", value="ピッチ（半音単位）", inline=True)
        embed.add_field(name="/volume <0-200>", value="音量調整（%）", inline=True)
        embed.add_field(name="/seek <時間>", value="再生位置へジャンプ (例 1:30)", inline=True)
        embed.add_field(name="/preset <名前>", value="エフェクト（ナイトコア等）", inline=True)
        embed.add_field(name="/remove・/move・/clear", value="キューの編集・クリア", inline=True)
        embed.add_field(
            name="/previous・/replay", value="前の曲・現在曲を先頭から再生", inline=True)
        embed.add_field(
            name="/history・/historyplay", value="履歴表示・履歴から追加", inline=True)
        embed.add_field(name="/favorite・/favorites", value="お気に入り保存・表示", inline=True)
        embed.add_field(name="/playfavorite・/unfavorite", value="お気に入り再生・削除", inline=True)
        embed.add_field(name="/settings", value="サーバー既定値（管理者）", inline=True)
        embed.add_field(name="/join・/leave", value="VCに参加・退出", inline=True)
        embed.add_field(name="/na-", value="効果音（同一曲中1回）", inline=True)
        embed.add_field(name="/sound <名前>", value="サウンドボード再生", inline=True)
        embed.add_field(name="/refresh", value="ニコニコCookie更新", inline=True)
        embed.add_field(name="再生中ボタン", value="🐢🐇 速度 / 🔽🔼 ピッチ / 🎚️ リセット", inline=False)
        embed.add_field(name="メッセージトリガー", value="`んあー` / `んあーと` で効果音", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop playing and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        cancel_idle_task(interaction.guild.id)
        state = guild_states.get(interaction.guild.id)
        if state:
            await retire_now_playing(state)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        cleanup_guild_state(interaction.guild.id)
        await interaction.followup.send("停止してキューをクリアしました。")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            mark_paused(get_state(interaction.guild.id), vc)
            vc.pause()
            await interaction.response.send_message("一時停止しました。")
        else:
            await interaction.response.send_message("再生していません。")

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            mark_resumed(get_state(interaction.guild.id))
            await interaction.response.send_message("再開しました。")
        else:
            await interaction.response.send_message("一時停止していません。")

    @app_commands.command(name="nowplaying", description="Show current playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if not state.current_song:
            await interaction.response.send_message("再生中の曲はありません。")
            return
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        elapsed = current_elapsed(vc, state) if vc and (vc.is_playing() or vc.is_paused()) else None
        embed = create_now_playing_embed(state.current_song, elapsed=elapsed, state=state)
        await retire_now_playing(state)
        state.np_message = await interaction.followup.send(
            embed=embed, view=MusicControls(), wait=True)
        start_np_updater(interaction.guild.id)

    async def _play_named_sound(self, interaction: discord.Interaction, name: str, success_msg: str):
        """Shared handler for /na- and /sound: validate, play, and reply."""
        state = get_state(interaction.guild.id)
        vc = state.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("VCに接続していません。")
            return
        if not vc.is_playing():
            await interaction.response.send_message("再生していません。")
            return
        path = resolve_sound(name)
        if not path:
            await interaction.response.send_message("効果音ファイルが見つかりません。")
            return
        if state.is_playing_sound or state.sound_used:
            await interaction.response.send_message("同一楽曲再生中に1度しか流せません")
            return
        logger.info(f"Playing sound effect '{name}' via slash command")
        if play_sound_effect(interaction.guild.id, path):
            await interaction.response.send_message(success_msg)
        else:
            await interaction.response.send_message("効果音の再生に失敗しました。")

    @app_commands.command(name="na-", description="ンアッー!(≧д≦)")
    async def na_command(self, interaction: discord.Interaction):
        await self._play_named_sound(interaction, "na-", "ンアッー!")

    @app_commands.command(name="sound", description="効果音を再生（同一曲中に1回）")
    @app_commands.describe(name="効果音の名前")
    async def sound_cmd(self, interaction: discord.Interaction, name: str):
        await self._play_named_sound(interaction, name, f"🔊 {name}")

    @sound_cmd.autocomplete("name")
    async def sound_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.lower()
        return [app_commands.Choice(name=n, value=n)
                for n in list_sound_names() if current in n.lower()][:25]

    @app_commands.command(name="refresh", description="Refresh niconico cookies")
    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        cookies.last_cookie_refresh = 0
        success = await asyncio.get_running_loop().run_in_executor(
            None, cookies.refresh_nico_cookies_sync, True)
        if success:
            await interaction.followup.send("Cookieを更新しました！", ephemeral=True)
        else:
            await interaction.followup.send("Cookieの更新に失敗しました", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Additive listener: the Bot's default on_message still runs
        # process_commands, so we only handle the sound-effect trigger here.
        if message.author.bot or not message.guild:
            return
        if message.content not in ("んあー", "んあーと"):
            return
        path = resolve_sound("na-")
        if not path:
            logger.warning("Sound file 'na-' not found")
            return
        state = guild_states.get(message.guild.id)
        user_voice = getattr(message.author, "voice", None)
        if (not state or not state.voice_client or not user_voice
                or user_voice.channel != state.voice_client.channel):
            return
        logger.info(f"Playing sound effect for trigger: {message.content}")
        play_sound_effect(message.guild.id, path)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            if self.bot.user and member.id == self.bot.user.id and after.channel is None:
                cleanup_guild_state(member.guild.id)
            return

        guild = member.guild
        state = guild_states.get(guild.id)
        if not state or not state.voice_client or not state.voice_client.is_connected():
            return

        bot_channel = state.voice_client.channel
        human_members = [m for m in bot_channel.members if not m.bot]
        if len(human_members) == 0:
            logger.info("All users left the voice channel, disconnecting bot")
            cancel_idle_task(guild.id)
            text_channel = resolve_text_channel(guild, state.current_song or {})
            await state.voice_client.disconnect()
            cleanup_guild_state(guild.id)
            if text_channel:
                await text_channel.send("誰も居なくなったので退出しました。")

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        # Bot kicked/banned or the guild was deleted — drop any leftover state
        # (queued tasks, now-playing message ref) so it doesn't linger forever.
        cleanup_guild_state(guild.id)
        persistence.delete_guild_data(guild.id)
        cookies.delete_guild_session(guild.id)
