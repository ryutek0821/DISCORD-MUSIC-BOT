"""Logic tests for the inmermusic package (no Discord connection required).

Run either way:
    python tests/test_features.py     # standalone, no extra deps
    pytest tests/test_features.py     # if pytest is installed

Importing the package is safe: bot.run() is only called from main.py under
__name__ == "__main__", so importing here never starts the bot.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inmermusic import audio, config, cookies, playback, ui, util


def test_build_audio_filter_defaults():
    # All defaults -> no filter (lossless passthrough)
    assert audio.build_audio_filter(1.0, 0, 100, "off") is None


def test_build_audio_filter_volume():
    assert "volume=2.000" in audio.build_audio_filter(1.0, 0, 200, "off")
    assert "volume=0.500" in audio.build_audio_filter(1.0, 0, 50, "off")


def test_build_audio_filter_effects():
    assert "bass=g=12" in audio.build_audio_filter(1.0, 0, 100, "bassboost")
    assert "apulsator" in audio.build_audio_filter(1.0, 0, 100, "8d")
    assert "lowpass" in audio.build_audio_filter(1.0, 0, 100, "lofi")


def test_build_audio_filter_speed_pitch():
    assert "atempo" in audio.build_audio_filter(1.5, 0, 100, "off")
    af = audio.build_audio_filter(1.0, 3, 100, "off")
    # pitch via asetrate shifts speed; atempo compensates back
    assert "asetrate" in af and "atempo" in af


def test_build_audio_filter_combo():
    af = audio.build_audio_filter(1.25, 3, 150, "bassboost")
    for token in ("asetrate", "atempo", "bass=g=12", "volume=1.500"):
        assert token in af, token


def test_parse_time():
    assert util.parse_time("90") == 90
    assert util.parse_time("1:30") == 90
    assert util.parse_time("1:02:03") == 3723
    assert util.parse_time("0:05") == 5
    assert util.parse_time("abc") is None
    assert util.parse_time("") is None
    assert util.parse_time("1:2:3:4") is None


def test_parse_time_rejects_non_finite_values():
    """NaN/Infinity must not become seek positions."""
    assert util.parse_time("nan") is None
    assert util.parse_time("inf") is None
    assert util.parse_time("-inf") is None
    assert util.parse_time("1:nan") is None


def test_validate_media_query_allowlist():
    audio.validate_query("夜に駆ける")
    audio.validate_query("https://www.youtube.com/watch?v=abc")
    audio.validate_query("https://www.nicovideo.jp/watch/sm9")
    for query in ("http://127.0.0.1:8080/", "https://example.com/a", "file:///tmp/a"):
        try:
            audio.validate_query(query)
        except ValueError:
            pass
        else:
            raise AssertionError(f"query unexpectedly accepted: {query}")


def test_fmt_duration():
    assert util.fmt_duration(90) == "1:30"
    assert util.fmt_duration(3723) == "1:02:03"
    assert util.fmt_duration(5) == "0:05"
    assert util.fmt_duration(-10) == "0:00"


def test_make_progress_bar():
    bar = ui.make_progress_bar(90, 180)
    assert "1:30" in bar and "3:00" in bar
    assert "\U0001f518" in bar              # position marker present
    assert ui.make_progress_bar(10, 0) == ""   # unknown duration -> empty
    assert ui.make_progress_bar(9999, 180) != ""  # clamps over 100%


def test_write_netscape_cookies():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    original = config.COOKIE_FILE
    try:
        config.COOKIE_FILE = path
        count = cookies.write_netscape_cookies([
            {"name": "user_session", "value": "abc", "domain": "nicovideo.jp",
             "path": "/", "secure": True, "expiry": 1999999999},
            {"name": "lang", "value": "ja"},  # minimal record -> defaults applied
        ])
        assert count == 2
        with open(path) as f:
            content = f.read()
        assert content.startswith("# Netscape HTTP Cookie File\n")
        rows = [ln.split("\t") for ln in content.splitlines()
                if ln and not ln.startswith("#")]
        assert len(rows) == 2
        # Full record: leading dot added, secure TRUE, expiry preserved.
        assert rows[0] == [".nicovideo.jp", "TRUE", "/", "TRUE", "1999999999",
                           "user_session", "abc"]
        # Minimal record: domain/path/secure/expiry fall back to defaults.
        assert rows[1] == [".nicovideo.jp", "TRUE", "/", "FALSE", "0", "lang", "ja"]
        # Cookie file must stay private.
        assert (os.stat(path).st_mode & 0o777) == 0o600
    finally:
        config.COOKIE_FILE = original
        if os.path.exists(path):
            os.remove(path)


def test_cleanup_temp_files():
    import tempfile
    import time
    d = tempfile.mkdtemp()
    old = os.path.join(d, "dl_old.m4a")
    recent = os.path.join(d, "dl_recent.m4a")
    keep = os.path.join(d, "keep.txt")
    for p in (old, recent, keep):
        with open(p, "w") as f:
            f.write("x")
    past = time.time() - 7200  # 2h old, past the 1h threshold
    os.utime(old, (past, past))
    original = audio.tempfile.gettempdir
    try:
        audio.tempfile.gettempdir = lambda: d
        removed = audio.cleanup_temp_files(max_age=3600)
    finally:
        audio.tempfile.gettempdir = original
    assert removed == 1
    assert not os.path.exists(old)       # aged dl_* removed
    assert os.path.exists(recent)        # fresh dl_* kept
    assert os.path.exists(keep)          # non-dl_* untouched
    for p in (recent, keep):
        os.remove(p)
    os.rmdir(d)


def test_preset_integrity():
    # Presets and labels must cover exactly the same set of keys, so a new
    # preset can't ship without a UI label (or vice versa).
    assert set(config.EFFECT_PRESETS) == set(config.EFFECT_LABELS)
    # Every preset references a defined effect filter and carries a full spec.
    for key, preset in config.EFFECT_PRESETS.items():
        assert preset["effect"] in config.EFFECT_FILTERS, key
        assert {"speed", "pitch", "effect"} <= set(preset), key
    # Every preset has a dropdown emoji, within Discord's 25-option cap.
    assert set(config.EFFECT_EMOJI) == set(config.EFFECT_PRESETS)
    assert len(config.EFFECT_PRESETS) <= 25


def test_preset_filters_emitted():
    # Each effect's filter tokens must actually appear in the built -af chain.
    for key, preset in config.EFFECT_PRESETS.items():
        af = audio.build_audio_filter(preset["speed"], preset["pitch"], 100, preset["effect"])
        for token in config.EFFECT_FILTERS[preset["effect"]]:
            assert af and token in af, (key, token)


def test_preset_ui_in_sync():
    # Dropdown options and slash choices are generated from config; verify they
    # cover exactly the presets so the UI can't drift out of sync.
    from inmermusic import ui
    from inmermusic.cog import _PRESET_CHOICES
    keys = set(config.EFFECT_PRESETS)
    assert {o.value for o in ui._PRESET_OPTIONS} == keys
    assert {c.value for c in _PRESET_CHOICES} == keys


def test_cog_registration():
    # Importing the bot/cog wires every slash command; verify the full set is
    # present (CI can't start the bot, so this guards the cog refactor).
    from inmermusic.bot import bot
    from inmermusic.cog import MusicCog
    names = {c.name for c in MusicCog(bot).get_app_commands()}
    expected = {
        "play", "skip", "queue", "loop", "shuffle", "speed", "pitch", "seek",
        "volume", "preset", "remove", "move", "clear", "join", "leave", "help",
        "stop", "pause", "resume", "nowplaying", "na-", "sound", "refresh",
    }
    assert names == expected, (expected - names, names - expected)


def test_move_queue_item():
    from inmermusic.state import move_queue_item
    q = ["a", "b", "c", "d"]
    assert move_queue_item(q, 1, 3) is True
    assert q == ["b", "c", "a", "d"]      # first -> 3rd
    assert move_queue_item(q, 4, 1) is True
    assert q == ["d", "b", "c", "a"]      # last -> first
    # No-ops and out-of-range leave the queue unchanged.
    assert move_queue_item(q, 2, 2) is False
    assert move_queue_item(q, 0, 1) is False
    assert move_queue_item(q, 1, 99) is False
    assert move_queue_item(["only"], 1, 1) is False
    assert q == ["d", "b", "c", "a"]


def test_soundboard_helpers():
    import tempfile
    d = tempfile.mkdtemp()
    for f in ("na-.mp3", "boo.mp3", "notes.txt"):
        open(os.path.join(d, f), "w").close()
    original = config.SOUNDS_DIR
    try:
        config.SOUNDS_DIR = d
        assert config.list_sound_names() == ["boo", "na-"]   # sorted, .mp3 only
        assert config.resolve_sound("na-") == os.path.join(d, "na-.mp3")
        assert config.resolve_sound("missing") is None
        # Path traversal must be rejected.
        assert config.resolve_sound("../secret") is None
        assert config.resolve_sound("a/b") is None
        assert config.resolve_sound("..") is None
        assert config.resolve_sound("") is None
    finally:
        config.SOUNDS_DIR = original
        for f in ("na-.mp3", "boo.mp3", "notes.txt"):
            os.remove(os.path.join(d, f))
        os.rmdir(d)


class FakeVoiceClient:
    """Minimal stand-in for discord.VoiceClient, just enough for play_next."""
    def __init__(self):
        self.played = []
        self._playing = False

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return False

    def play(self, source, after=None):
        self._playing = True
        self.played.append(source)

    def stop(self):
        self._playing = False


def test_advance_queue_decision_tree():
    # play_next itself is irrelevant here; only the queue mutation matters.
    from inmermusic.state import get_state, guild_states

    guild_id = 900101
    calls = []

    async def fake_play_next(gid, announce=True):
        calls.append(gid)

    original = playback.play_next
    playback.play_next = fake_play_next
    try:
        state = get_state(guild_id)

        # skip_flag overrides loop_mode: the finished song is dropped either way.
        state.skip_flag = True
        state.loop_mode = "song"
        state.queue = []
        finished = {"title": "a", "local_file": "/tmp/a"}
        asyncio.run(playback.advance_queue(guild_id, finished))
        assert state.skip_flag is False
        assert state.queue == []

        # loop_mode == "song": finished song is reinserted at the front.
        state.loop_mode = "song"
        state.queue = [{"title": "next"}]
        finished = {"title": "b", "local_file": "/tmp/b"}
        asyncio.run(playback.advance_queue(guild_id, finished))
        assert state.queue[0] is finished
        assert finished["local_file"] is None  # temp file already cleaned up
        assert state.queue[1]["title"] == "next"

        # loop_mode == "queue": finished song is appended at the tail.
        state.loop_mode = "queue"
        state.queue = [{"title": "next"}]
        finished = {"title": "c", "local_file": "/tmp/c"}
        asyncio.run(playback.advance_queue(guild_id, finished))
        assert state.queue[-1] is finished
        assert finished["local_file"] is None

        # loop_mode == "off": the finished song is simply dropped.
        state.loop_mode = "off"
        state.queue = [{"title": "next"}]
        finished = {"title": "d"}
        asyncio.run(playback.advance_queue(guild_id, finished))
        assert state.queue == [{"title": "next"}]

        assert calls == [guild_id] * 4
    finally:
        playback.play_next = original
        guild_states.pop(guild_id, None)


def test_play_next_skip_and_drain():
    from inmermusic.state import get_state, guild_states

    def fake_make_audio_source(song, state, seek=0.0):
        if song["title"] == "bad":
            raise RuntimeError("boom")
        return "SENTINEL_SOURCE"

    skip_calls = []

    async def fake_notify_skip(gid, song, reason):
        skip_calls.append((song["title"], reason))

    async def fake_announce_now_playing(gid):
        pass

    def fake_start_np_updater(gid, interval=None):
        pass

    async def fake_schedule_disconnect(gid):
        pass

    orig_make_audio_source = playback.make_audio_source
    orig_notify_skip = playback.notify_skip
    orig_announce_now_playing = playback.announce_now_playing
    orig_start_np_updater = playback.start_np_updater
    orig_schedule_disconnect = playback.schedule_disconnect
    playback.make_audio_source = fake_make_audio_source
    playback.notify_skip = fake_notify_skip
    playback.announce_now_playing = fake_announce_now_playing
    playback.start_np_updater = fake_start_np_updater
    playback.schedule_disconnect = fake_schedule_disconnect

    guild_a, guild_b = 900102, 900103
    try:
        # [bad] -> every song fails, queue drains, current_song stays None.
        state = get_state(guild_a)
        state.voice_client = FakeVoiceClient()
        state.queue = [{"title": "bad", "needs_local": False}]
        asyncio.run(playback.play_next(guild_a))
        assert state.current_song is None
        assert skip_calls == [("bad", "再生エラー")]

        # [bad, good] -> the bad song is skipped, the good one plays.
        skip_calls.clear()
        state = get_state(guild_b)
        state.voice_client = FakeVoiceClient()
        good_song = {"title": "good", "needs_local": False}
        state.queue = [{"title": "bad", "needs_local": False}, good_song]
        asyncio.run(playback.play_next(guild_b))
        assert state.current_song == good_song
        assert skip_calls == [("bad", "再生エラー")]
    finally:
        playback.make_audio_source = orig_make_audio_source
        playback.notify_skip = orig_notify_skip
        playback.announce_now_playing = orig_announce_now_playing
        playback.start_np_updater = orig_start_np_updater
        playback.schedule_disconnect = orig_schedule_disconnect
        guild_states.pop(guild_a, None)
        guild_states.pop(guild_b, None)


def test_cleanup_guild_state():
    from inmermusic.state import get_state, guild_states

    guild_id = 900104

    async def _sleep_forever():
        await asyncio.sleep(100)

    async def _run():
        state = get_state(guild_id)
        state.idle_task = asyncio.create_task(_sleep_forever())
        state.np_updater = asyncio.create_task(_sleep_forever())
        state.reapply_task = asyncio.create_task(_sleep_forever())
        state.np_message = object()
        await asyncio.sleep(0)  # let the tasks actually start running

        tasks = (state.idle_task, state.np_updater, state.reapply_task)
        playback.cleanup_guild_state(guild_id)
        assert guild_id not in guild_states
        assert state.np_message is None

        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        assert all(t.cancelled() for t in tasks)

    try:
        asyncio.run(_run())
    finally:
        guild_states.pop(guild_id, None)


def test_stale_after_callback_does_not_recreate_state():
    """A callback from a stopped track must not resurrect GuildState."""
    from inmermusic.state import get_state, guild_states

    class CapturingVoiceClient(FakeVoiceClient):
        def play(self, source, after=None):
            super().play(source, after=after)
            self.after = after

    guild_id = 900105
    original_make = playback.make_audio_source
    original_announce = playback.announce_now_playing
    original_updater = playback.start_np_updater

    async def fake_announce(_guild_id):
        return None

    playback.make_audio_source = lambda song, state, seek=0.0: "SOURCE"
    playback.announce_now_playing = fake_announce
    playback.start_np_updater = lambda *args, **kwargs: None

    async def scenario():
        state = get_state(guild_id)
        state.voice_client = CapturingVoiceClient()
        state.queue = [{"title": "stale", "needs_local": False}]
        await playback.play_next(guild_id)
        callback = state.voice_client.after
        playback.cleanup_guild_state(guild_id)
        callback(None)
        await asyncio.sleep(0.05)
        assert guild_id not in guild_states

    try:
        asyncio.run(scenario())
    finally:
        playback.make_audio_source = original_make
        playback.announce_now_playing = original_announce
        playback.start_np_updater = original_updater
        guild_states.pop(guild_id, None)


def test_friendly_extract_error():
    f = util.friendly_extract_error
    assert f("ERROR: Private video. Sign in if you've been granted access") == \
        "ログインが必要な動画のため再生できません。"
    assert f("This video requires age verification") == \
        "年齢制限付きの動画のため再生できません。"
    assert f("This video is not available in your country due to copyright") == \
        "地域制限により再生できません。"
    assert f("Video unavailable. This video has been removed") == \
        "動画が削除・非公開のため見つかりません。"
    assert f("HTTP Error 504: Connection timed out") == \
        "ネットワークエラーです。時間をおいて再試行してください。"
    assert f("some completely different failure") == \
        "取得に失敗しました。URLやキーワードを確認してください。"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print("PASS " + t.__name__)
        except AssertionError as e:
            failed.append(t.__name__)
            print("FAIL " + t.__name__ + ((": " + str(e)) if str(e) else ""))
    print("SUMMARY %d/%d passed" % (len(tests) - len(failed), len(tests)))
    sys.exit(1 if failed else 0)
