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


def test_initial_now_playing_message_waits_for_response():
    """The first /play followup must return the Message used by its updater."""
    import ast
    import inspect
    import textwrap

    from inmermusic.cog import MusicCog

    tree = ast.parse(textwrap.dedent(inspect.getsource(MusicCog.play.callback)))
    assignment = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "np_message"
                for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Await)
    call = assignment.value.value
    wait = next((kw for kw in call.keywords if kw.arg == "wait"), None)
    assert wait is not None and isinstance(wait.value, ast.Constant)
    assert wait.value.value is True


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

    async def fake_notify_skip(gid, song, reason, expected_state=None):
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


def test_validate_query_empty_message():
    # An empty/whitespace-only query must get its own message, not the
    # "too long" one.
    for empty in ("", "   ", "\t\n"):
        try:
            audio.validate_query(empty)
        except ValueError as e:
            assert str(e) == "検索語を入力してください", (empty, str(e))
        else:
            raise AssertionError(f"empty query unexpectedly accepted: {empty!r}")
    try:
        audio.validate_query("a" * 201)
    except ValueError as e:
        assert str(e) == "検索語が長すぎます"
    else:
        raise AssertionError("overlong query unexpectedly accepted")


def test_drop_abandoned_state_keeps_active_session():
    # Regression for review item 1: a /play whose extraction failed must
    # never destroy a GuildState that's already hosting a session (a song
    # popped into current_song, even with an empty queue) or one that
    # predates this call.
    from inmermusic.cog import _drop_abandoned_state
    from inmermusic.state import get_state, guild_states

    guild_id = 900110
    try:
        # created=True but a song is already playing (queue empty) -> keep.
        state = get_state(guild_id)
        state.current_song = {"title": "playing"}
        state.queue = []
        _drop_abandoned_state(guild_id, state, created=True)
        assert guild_states.get(guild_id) is state

        # created=False (state pre-existed this /play call) -> never drop,
        # even though it looks empty/untouched.
        state.current_song = None
        _drop_abandoned_state(guild_id, state, created=False)
        assert guild_states.get(guild_id) is state

        # created=True and genuinely untouched (this call's own state) -> drop.
        _drop_abandoned_state(guild_id, state, created=True)
        assert guild_id not in guild_states
    finally:
        guild_states.pop(guild_id, None)


def test_restart_song_download_failure_resets_flag_and_skips_reinsert():
    # Regression for review item 2: a failed resume-download after a sound
    # effect must clear is_playing_sound and must NOT be reinserted into the
    # queue even under loop_mode="song" (it's a forced skip, not a normal
    # end-of-song).
    from inmermusic.state import get_state, guild_states

    guild_id = 900111
    skip_reasons = []
    play_next_calls = []

    async def fake_notify_skip(gid, song, reason, expected_state=None):
        skip_reasons.append(reason)

    async def fake_play_next(gid, announce=True):
        play_next_calls.append(gid)

    original_download = playback.download_audio
    original_notify_skip = playback.notify_skip
    original_play_next = playback.play_next
    playback.download_audio = lambda url: None  # simulates a failed download
    playback.notify_skip = fake_notify_skip
    playback.play_next = fake_play_next

    try:
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        song = {"title": "resume-me", "needs_local": True, "local_file": None,
                "url": "http://example.com/x"}
        state.current_song = song
        state.loop_mode = "song"
        state.queue = []
        state.is_playing_sound = True
        state.resume_position = 5.0

        asyncio.run(playback.restart_song(guild_id, expected_state=state))

        assert state.is_playing_sound is False
        assert song not in state.queue      # not reinserted despite loop_mode="song"
        assert state.queue == []
        assert skip_reasons == ["読み込み失敗"]
        assert play_next_calls == [guild_id]
    finally:
        playback.download_audio = original_download
        playback.notify_skip = original_notify_skip
        playback.play_next = original_play_next
        guild_states.pop(guild_id, None)


def test_notify_skip_after_cleanup_does_not_recreate_state():
    # Regression for review item 3: notify_skip must use guild_states.get()
    # (never get_state()), so a callback racing a cleanup_guild_state() can't
    # resurrect a dropped GuildState.
    from inmermusic.state import get_state, guild_states

    guild_id = 900112
    try:
        state = get_state(guild_id)
        playback.cleanup_guild_state(guild_id)
        assert guild_id not in guild_states

        asyncio.run(playback.notify_skip(guild_id, {"title": "x"}, "reason",
                                         expected_state=state))
        assert guild_id not in guild_states

        # Also without an expected_state — guild_states.get() alone must
        # short-circuit before ever touching state.voice_client.
        asyncio.run(playback.notify_skip(guild_id, {"title": "x"}, "reason"))
        assert guild_id not in guild_states
    finally:
        guild_states.pop(guild_id, None)


def test_download_audio_removes_temp_dir_on_failure():
    # Regression for review item 5: a failed download must not leave its
    # per-request dl_* directory behind.
    import tempfile as _tempfile

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            raise RuntimeError("boom")

    d = _tempfile.mkdtemp()
    original_gettempdir = audio.tempfile.gettempdir
    original_ydl = audio.yt_dlp.YoutubeDL
    original_cookie_file = config.COOKIE_FILE
    audio.tempfile.gettempdir = lambda: d
    audio.yt_dlp.YoutubeDL = FakeYDL
    config.COOKIE_FILE = None  # avoid touching the real cookie file path
    try:
        result = audio.download_audio("https://example.com/video")
        assert result is None
        leftovers = [n for n in os.listdir(d) if n.startswith("dl_")]
        assert leftovers == []
    finally:
        audio.tempfile.gettempdir = original_gettempdir
        audio.yt_dlp.YoutubeDL = original_ydl
        config.COOKIE_FILE = original_cookie_file
        os.rmdir(d)


def test_cleanup_late_download_removes_dir_after_timeout():
    """Regression for review item 5.

    A download that finishes AFTER its asyncio.wait_for timeout must still
    have its temp directory removed. This drives the real _play_next timeout
    path rather than calling _cleanup_late_download directly, because the bug
    lived in the wait_for/add_done_callback interaction: wait_for cancels its
    argument before raising, so without asyncio.shield() the callback fires
    immediately on an already-cancelled future and cleans up nothing.
    """
    import tempfile as _tempfile
    import time as _time
    from inmermusic.state import get_state, guild_states

    guild_id = 900106
    made = {}

    def slow_download(url):
        d = _tempfile.mkdtemp(prefix="dl_")
        made["dir"] = d
        path = os.path.join(d, "video.m4a")
        with open(path, "w") as f:
            f.write("x")
        _time.sleep(0.5)  # finishes well after the wait_for timeout below
        return path

    async def fake_notify_skip(gid, song, reason, expected_state=None):
        return None

    original_download = playback.download_audio
    original_timeout = playback.DOWNLOAD_TIMEOUT
    original_notify = playback.notify_skip
    playback.download_audio = slow_download
    playback.DOWNLOAD_TIMEOUT = 0.1
    playback.notify_skip = fake_notify_skip

    async def scenario():
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.queue = [{"title": "slow", "needs_local": True,
                        "url": "https://www.youtube.com/watch?v=x"}]
        await playback.play_next(guild_id)
        # Timed out and skipped; nothing should have started playing.
        assert state.voice_client.played == []
        assert made["dir"] is not None
        # Let the abandoned executor thread finish and the done-callback run.
        for _ in range(40):
            if not os.path.isdir(made["dir"]):
                break
            await asyncio.sleep(0.05)
        assert not os.path.isdir(made["dir"]), \
            "timed-out download left its temp dir behind"

    try:
        asyncio.run(scenario())
    finally:
        playback.download_audio = original_download
        playback.DOWNLOAD_TIMEOUT = original_timeout
        playback.notify_skip = original_notify
        playback.cleanup_guild_state(guild_id)
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
    # "webpage" and "API page" contain the letters "age", but are not
    # evidence of an age restriction.
    assert f("Unable to download webpage: HTTP Error 500") == \
        "ネットワークエラーです。時間をおいて再試行してください。"
    assert f("Unable to extract API page") == \
        "取得に失敗しました。URLやキーワードを確認してください。"
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
