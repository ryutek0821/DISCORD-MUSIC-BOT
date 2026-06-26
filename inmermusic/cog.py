"""All slash commands and the message/voice listeners, as a single cog."""
import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

from . import cookies
from .audio import (current_elapsed, extract_audio_url, extract_playlist,
                    is_playlist_url, swap_source_at)
from .config import EFFECT_PRESETS, list_sound_names, logger, resolve_sound
from .playback import (cancel_idle_task, cancel_np_updater, cancel_reapply,
                       play_next, play_sound_effect, refresh_now_playing,
                       schedule_reapply, start_np_updater)
from .state import get_state, guild_states, move_queue_item
from .ui import MusicControls, create_now_playing_embed, create_queued_embed
from .util import fmt_duration, parse_time


class MusicCog(commands.Cog):
    """NicoNico/YouTube music playback commands and triggers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _connect_for_play(self, interaction: discord.Interaction):
        """Connect to / move into the user's VC. Returns the voice client, or
        None after replying with an error."""
        if not interaction.user.voice:
            await interaction.followup.send("VCに参加してください。")
            return None
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        state = get_state(interaction.guild.id)
        if not vc:
            try:
                vc = await channel.connect(timeout=15)
                state.voice_client = vc
            except Exception as e:
                await interaction.followup.send(f"VC接続失敗: {str(e)}")
                return None
        elif vc.channel != channel:
            try:
                await vc.move_to(channel)
            except Exception as e:
                await interaction.followup.send(f"チャンネル移動失敗: {str(e)}")
                return None
        return vc

    @app_commands.command(name="play", description="Play from NicoNico/YouTube (URL, playlist, or search)")
    @app_commands.describe(query="NicoNico/YouTube URL, playlist URL, or search keyword")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("VCに参加してください。")
            return

        await interaction.response.defer()
        loop = asyncio.get_running_loop()

        if is_playlist_url(query):
            await self._play_playlist(interaction, query, loop)
            return

        try:
            song = await asyncio.wait_for(
                loop.run_in_executor(None, extract_audio_url, query),
                timeout=60
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("曲の取得がタイムアウトしました。")
            return
        except Exception as e:
            await interaction.followup.send(f"曲が見つかりません: {str(e)}")
            return

        song["text_channel_id"] = interaction.channel.id
        song["requester"] = interaction.user.display_name

        vc = await self._connect_for_play(interaction)
        if not vc:
            return

        cancel_idle_task(interaction.guild.id)

        state = get_state(interaction.guild.id)
        state.queue.append(song)

        if vc.is_playing() or vc.is_paused():
            embed = create_queued_embed(song, len(state.queue))
            await interaction.followup.send(embed=embed)
        else:
            state.current_song = song
            embed = create_now_playing_embed(song, elapsed=0.0, state=state)
            state.np_message = await interaction.followup.send(
                embed=embed, view=MusicControls()
            )
            start_np_updater(interaction.guild.id)
            # /play already announced this song, so don't re-announce in play_next.
            await play_next(interaction.guild.id, announce=False)

    async def _play_playlist(self, interaction: discord.Interaction, query: str, loop):
        try:
            songs, pl_title = await asyncio.wait_for(
                loop.run_in_executor(None, extract_playlist, query),
                timeout=90
            )
        except asyncio.TimeoutError:
            await interaction.followup.send("プレイリストの取得がタイムアウトしました。")
            return
        except Exception as e:
            await interaction.followup.send(f"プレイリストの取得に失敗しました: {str(e)}")
            return
        if not songs:
            await interaction.followup.send("プレイリストから曲を取得できませんでした。")
            return

        vc = await self._connect_for_play(interaction)
        if not vc:
            return

        cancel_idle_task(interaction.guild.id)
        state = get_state(interaction.guild.id)
        for s in songs:
            s["text_channel_id"] = interaction.channel.id
            s["requester"] = interaction.user.display_name
        was_playing = vc.is_playing() or vc.is_paused()
        state.queue.extend(songs)
        await interaction.followup.send(f"🎶 **{pl_title}** から {len(songs)}曲を追加しました。")
        if not was_playing:
            # Nothing playing yet — start the first track (announces to the channel).
            await play_next(interaction.guild.id)

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
        total = len(state.queue)
        lines = [f"{i+1}. **[{s['title']}]({s['url']})** (by {s.get('requester', '不明')})"
                 for i, s in enumerate(state.queue[:20])]
        desc = "\n".join(lines)
        if total > 20:
            desc += f"\n…他 {total - 20} 曲"
        embed = discord.Embed(title=f"キュー（{total}曲）", description=desc, color=0x00ff00)
        total_dur = sum((s.get("duration") or 0) for s in state.queue)
        if total_dur:
            embed.set_footer(text=f"合計再生時間: {fmt_duration(total_dur)}")
        await interaction.response.send_message(embed=embed)

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
    @app_commands.choices(name=[
        app_commands.Choice(name="オフ（通常）", value="off"),
        app_commands.Choice(name="ナイトコア", value="nightcore"),
        app_commands.Choice(name="ベイパーウェイブ", value="vaporwave"),
        app_commands.Choice(name="低音ブースト", value="bassboost"),
        app_commands.Choice(name="8Dオーディオ", value="8d"),
        app_commands.Choice(name="Lo-Fi", value="lofi"),
        app_commands.Choice(name="エコー", value="echo"),
        app_commands.Choice(name="リバーブ", value="reverb"),
        app_commands.Choice(name="トレモロ", value="tremolo"),
        app_commands.Choice(name="ボーカルカット", value="karaoke"),
        app_commands.Choice(name="高音ブースト", value="trebleboost"),
    ])
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
        await interaction.response.send_message(
            f"↕️ **{title}** を #{from_pos} → #{to_pos} に移動しました。"
        )

    @app_commands.command(name="clear", description="Clear the queue without disconnecting")
    async def clear_cmd(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        count = len(state.queue)
        state.queue.clear()
        await interaction.response.send_message(
            f"🧹 キューをクリアしました（{count}曲）。再生中の曲は継続します。"
        )

    @app_commands.command(name="join", description="Join your voice channel")
    async def join_cmd(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("先にVCに参加してください。")
            return
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
            await interaction.response.send_message(f"VC接続失敗: {str(e)}")
            return
        await interaction.response.send_message(f"🔊 接続しました: **{channel.name}**")

    @app_commands.command(name="leave", description="Disconnect from the voice channel")
    async def leave_cmd(self, interaction: discord.Interaction):
        cancel_idle_task(interaction.guild.id)
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("VCに接続していません。")
            return
        vc.stop()
        await vc.disconnect()
        state = guild_states.get(interaction.guild.id)
        if state:
            cancel_np_updater(state)
        if interaction.guild.id in guild_states:
            del guild_states[interaction.guild.id]
        await interaction.response.send_message("👋 退出しました。")

    @app_commands.command(name="help", description="Show available commands")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(title="INMERMUSIC BOT コマンド一覧", color=0x00ff00)
        embed.add_field(name="/play <URL/キーワード>", value="ニコニコ/YouTube/プレイリスト/検索で再生", inline=False)
        embed.add_field(name="/skip", value="現在の曲をスキップ", inline=True)
        embed.add_field(name="/pause・/resume", value="一時停止・再開", inline=True)
        embed.add_field(name="/stop", value="停止してキュー削除・退出", inline=True)
        embed.add_field(name="/queue", value="キューを表示", inline=True)
        embed.add_field(name="/nowplaying", value="再生中の曲を表示", inline=True)
        embed.add_field(name="/loop <mode>", value="リピート (off/song/queue)", inline=True)
        embed.add_field(name="/shuffle", value="キューをシャッフル", inline=True)
        embed.add_field(name="/speed <0.5-2.0>", value="再生速度（ピッチ維持）", inline=True)
        embed.add_field(name="/pitch <-12〜12>", value="ピッチ（半音単位）", inline=True)
        embed.add_field(name="/volume <0-200>", value="音量調整（%）", inline=True)
        embed.add_field(name="/seek <時間>", value="再生位置へジャンプ (例 1:30)", inline=True)
        embed.add_field(name="/preset <名前>", value="エフェクト（ナイトコア等）", inline=True)
        embed.add_field(name="/remove <番号>", value="キューから曲を削除", inline=True)
        embed.add_field(name="/move <元> <先>", value="キューの曲を並べ替え", inline=True)
        embed.add_field(name="/clear", value="キューをクリア（再生は継続）", inline=True)
        embed.add_field(name="/join・/leave", value="VCに参加・退出", inline=True)
        embed.add_field(name="/na-", value="効果音（同一曲中1回）", inline=True)
        embed.add_field(name="/sound <名前>", value="サウンドボード再生", inline=True)
        embed.add_field(name="/refresh", value="ニコニコCookie更新", inline=True)
        embed.add_field(name="再生中ボタン", value="🐢🐇 速度 / 🔽🔼 ピッチ / 🎚️ リセット", inline=False)
        embed.add_field(name="メッセージトリガー", value="`んあー` / `んあーと` で効果音", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="stop", description="Stop playing and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        cancel_idle_task(interaction.guild.id)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
        state = guild_states.get(interaction.guild.id)
        if state:
            cancel_np_updater(state)
        if interaction.guild.id in guild_states:
            del guild_states[interaction.guild.id]
        await interaction.response.send_message("停止してキューをクリアしました。")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("一時停止しました。")
        else:
            await interaction.response.send_message("再生していません。")

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("再開しました。")
        else:
            await interaction.response.send_message("一時停止していません。")

    @app_commands.command(name="nowplaying", description="Show current playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = get_state(interaction.guild.id)
        if not state.current_song:
            await interaction.response.send_message("再生中の曲はありません。")
            return
        vc = interaction.guild.voice_client
        elapsed = current_elapsed(vc, state) if vc and (vc.is_playing() or vc.is_paused()) else None
        embed = create_now_playing_embed(state.current_song, elapsed=elapsed, state=state)
        await interaction.response.send_message(embed=embed, view=MusicControls())

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
        if state.is_playing_sound:
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
        logger.info(f"Playing sound effect for trigger: {message.content}")
        play_sound_effect(message.guild.id, path)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
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
            text_channel = None
            if state.current_song and state.current_song.get("text_channel_id"):
                text_channel = guild.get_channel(state.current_song["text_channel_id"])
            if not text_channel:
                text_channel = next((ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), None)
            cancel_np_updater(state)
            await state.voice_client.disconnect()
            if guild.id in guild_states:
                del guild_states[guild.id]
            if text_channel:
                await text_channel.send("誰も居なくなったので退出しました。")
