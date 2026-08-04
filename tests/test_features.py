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
import time

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


def test_pitch_filter_normalizes_input_sample_rate():
    """asetrate replaces the rate, so the input must be normalized first (#42)."""
    af = audio.build_audio_filter(1.0, 12, 100, "off")
    # The normalizing resample has to come before asetrate, otherwise the shift
    # is 48000*ratio/input_rate and a 44.1kHz source lands sharp.
    assert af.index("aresample=48000") < af.index("asetrate="), af
    assert "asetrate=96000" in af, af
    # pitch=0 must not pay for a resample it doesn't need.
    assert "aresample" not in (audio.build_audio_filter(1.5, 0, 100, "off") or "")
    assert audio.build_audio_filter(1.0, 0, 100, "off") is None


def test_pitch_filter_holds_duration_across_sample_rates():
    """+12 semitones at 1.0x must not change length, at 44.1kHz or 48kHz (#42)."""
    import shutil
    import subprocess
    import tempfile

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return  # ffmpeg isn't a test dependency; skip where it's unavailable

    def duration(path):
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, check=True)
        return float(out.stdout.strip())

    af = audio.build_audio_filter(1.0, 12, 100, "off")
    directory = tempfile.mkdtemp()
    try:
        for rate in (44100, 48000):
            src = os.path.join(directory, f"sine_{rate}.wav")
            dst = os.path.join(directory, f"out_{rate}.wav")
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                 f"sine=frequency=440:duration=2:sample_rate={rate}", src],
                check=True)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", src, "-af", af,
                 "-ar", "48000", dst],
                check=True)
            got = duration(dst)
            # Before the fix a 44.1kHz input came out ~1.84s instead of ~2.0s;
            # the remaining ~1% is atempo/resample rounding, not the rate bug.
            assert abs(got - 2.0) < 0.05, (rate, got)
    finally:
        shutil.rmtree(directory)


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
    assert audio.is_playlist_url(
        "https://www.youtube.com/playlist?list=PL123")
    assert audio.is_playlist_url(
        "https://www.nicovideo.jp/user/1/mylist/2")
    assert not audio.is_playlist_url(
        "https://www.youtube.com/watch?v=abc&list=PL123")
    assert not audio.is_playlist_url(
        "https://www.youtube.com/playlist?list=RDMM")


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


def test_guild_session_round_trip():
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    original = config.STATE_DIR
    config.STATE_DIR = d
    try:
        assert cookies.get_guild_session(12345) is None
        cookies.set_guild_session(12345, "session-value")
        assert cookies.get_guild_session(12345) == "session-value"
        cookies.delete_guild_session(12345)
        assert cookies.get_guild_session(12345) is None
        assert cookies.guild_cookie_file(12345) is None
        assert (os.stat(os.path.join(d, "guilds.db")).st_mode & 0o777) == 0o600
    finally:
        config.STATE_DIR = original
        shutil.rmtree(d)


def test_guild_session_store_error_policy():
    import shutil
    import sqlite3
    import tempfile

    d = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    original_connect = cookies._connect_guild_db
    original_warning = cookies.logger.warning
    config.STATE_DIR = d
    cookie_path = os.path.join(d, "cookies_12346.txt")
    with open(cookie_path, "w") as f:
        f.write("stale")
    warnings = []

    def unavailable_store():
        raise sqlite3.OperationalError("store unavailable")

    cookies._connect_guild_db = unavailable_store
    cookies.logger.warning = warnings.append
    try:
        delete_error = None
        try:
            cookies.delete_guild_session(12346)
        except Exception as e:
            delete_error = e
        assert delete_error is None
        assert not os.path.exists(cookie_path)
        assert any("store unavailable" in message for message in warnings)

        set_error = None
        try:
            cookies.set_guild_session(12346, "new-session")
        except sqlite3.OperationalError as e:
            set_error = e
        assert set_error is not None
    finally:
        cookies.logger.warning = original_warning
        cookies._connect_guild_db = original_connect
        config.STATE_DIR = original_state_dir
        shutil.rmtree(d)


def test_guild_cookie_file_format_and_permissions():
    import shutil
    import tempfile
    import time

    d = tempfile.mkdtemp()
    original = config.STATE_DIR
    config.STATE_DIR = d
    try:
        cookies.set_guild_session(23456, "guild-session")
        path = cookies.guild_cookie_file(23456)
        assert path == os.path.join(d, "cookies_23456.txt")
        with open(path) as f:
            content = f.read()
        rows = [ln.split("\t") for ln in content.splitlines()
                if ln and not ln.startswith("#")]
        assert rows[0][:4] == [".nicovideo.jp", "TRUE", "/", "TRUE"]
        assert int(rows[0][4]) > int(time.time())
        assert rows[0][5:] == ["user_session", "guild-session"]
        assert (os.stat(path).st_mode & 0o777) == 0o600
    finally:
        config.STATE_DIR = original
        shutil.rmtree(d)


def test_guild_cookie_file_is_loaded_and_sent_by_cookiejar():
    import http.cookiejar
    import shutil
    import tempfile
    import urllib.request

    d = tempfile.mkdtemp()
    original = config.STATE_DIR
    config.STATE_DIR = d
    try:
        cookies.set_guild_session(23457, "guild-session")
        path = cookies.guild_cookie_file(23457)
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(path)
        request = urllib.request.Request(
            "https://www.nicovideo.jp/watch/sm9")
        jar.add_cookie_header(request)
        assert request.get_header("Cookie") == \
            "user_session=guild-session"
    finally:
        config.STATE_DIR = original
        shutil.rmtree(d)


def test_guild_cookie_file_writes_only_when_session_changes():
    import concurrent.futures
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    original_writer = cookies.write_netscape_cookies
    config.STATE_DIR = d
    writes = []

    def counting_writer(records, output_path=None):
        writes.append(output_path)
        return original_writer(records, output_path=output_path)

    cookies.write_netscape_cookies = counting_writer
    try:
        cookies.set_guild_session(23458, "session-a")
        cookies.set_guild_session(23459, "session-b")
        guild_ids = [23458, 23459] * 8
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(cookies.guild_cookie_file, guild_ids))
        first_path = os.path.join(d, "cookies_23458.txt")
        second_path = os.path.join(d, "cookies_23459.txt")
        assert writes.count(first_path) == 1
        assert writes.count(second_path) == 1

        cookies.set_guild_session(23458, "session-a-updated")
        cookies.guild_cookie_file(23458)
        assert writes.count(first_path) == 2
        with open(first_path) as f:
            assert "user_session\tsession-a-updated" in f.read()
    finally:
        cookies.write_netscape_cookies = original_writer
        config.STATE_DIR = original_state_dir
        shutil.rmtree(d)


def test_unregistered_guild_uses_global_nico_credentials():
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    original_cookie_file = config.COOKIE_FILE
    original_email = audio.NICO_EMAIL
    original_password = audio.NICO_PASSWORD
    config.STATE_DIR = d
    config.COOKIE_FILE = os.path.join(d, "global-cookies.txt")
    audio.NICO_EMAIL = "global@example.com"
    audio.NICO_PASSWORD = "global-password"
    try:
        cookies.set_guild_session(34566, "another-guild-session")
        opts = audio.build_ydl_opts(
            "https://www.nicovideo.jp/watch/sm9", guild_id=34567)
        assert opts["cookiefile"] == config.COOKIE_FILE
        assert opts["username"] == "global@example.com"
        assert opts["password"] == "global-password"
    finally:
        config.STATE_DIR = original_state_dir
        config.COOKIE_FILE = original_cookie_file
        audio.NICO_EMAIL = original_email
        audio.NICO_PASSWORD = original_password
        shutil.rmtree(d)


def test_registered_guild_omits_nico_username_and_password():
    import shutil
    import tempfile

    d = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    original_cookie_file = config.COOKIE_FILE
    original_email = audio.NICO_EMAIL
    original_password = audio.NICO_PASSWORD
    config.STATE_DIR = d
    config.COOKIE_FILE = os.path.join(d, "global-cookies.txt")
    audio.NICO_EMAIL = "global@example.com"
    audio.NICO_PASSWORD = "global-password"
    try:
        cookies.set_guild_session(45678, "private-session")
        opts = audio.build_ydl_opts(
            "https://www.nicovideo.jp/watch/sm9", guild_id=45678)
        assert opts["cookiefile"] == os.path.join(d, "cookies_45678.txt")
        assert "username" not in opts
        assert "password" not in opts
    finally:
        config.STATE_DIR = original_state_dir
        config.COOKIE_FILE = original_cookie_file
        audio.NICO_EMAIL = original_email
        audio.NICO_PASSWORD = original_password
        shutil.rmtree(d)


def test_extract_reads_guild_session_once():
    import shutil
    import tempfile

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, url, download):
            return {
                "formats": [{
                    "acodec": "opus",
                    "vcodec": "none",
                    "url": "https://audio.example/track.opus",
                }],
                "title": "test",
                "duration": 60,
                "webpage_url": url,
            }

    d = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    original_connect = cookies._connect_guild_db
    original_ydl = audio.yt_dlp.YoutubeDL
    reads = []

    def counting_connect():
        reads.append(True)
        return original_connect()

    config.STATE_DIR = d
    cookies.set_guild_session(45679, "private-session")
    cookies._connect_guild_db = counting_connect
    audio.yt_dlp.YoutubeDL = FakeYDL
    try:
        audio.extract_audio_url(
            "https://www.nicovideo.jp/watch/sm9", guild_id=45679)
        assert reads == [True]
    finally:
        audio.yt_dlp.YoutubeDL = original_ydl
        cookies._connect_guild_db = original_connect
        config.STATE_DIR = original_state_dir
        shutil.rmtree(d)


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
    original = config.DOWNLOAD_DIR
    try:
        config.DOWNLOAD_DIR = d
        removed = audio.cleanup_temp_files(max_age=3600)
    finally:
        config.DOWNLOAD_DIR = original
    assert removed == 1
    assert not os.path.exists(old)       # aged dl_* removed
    assert os.path.exists(recent)        # fresh dl_* kept
    assert os.path.exists(keep)          # non-dl_* untouched
    for p in (recent, keep):
        os.remove(p)
    os.rmdir(d)


def test_download_and_cleanup_share_configured_directory():
    import tempfile as _tempfile
    import time as _time

    class FakeYDL:
        def __init__(self, opts):
            self.output_template = opts["outtmpl"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            filename = self.output_template.replace("%(id)s", "track").replace(
                "%(ext)s", "m4a")
            with open(filename, "w") as f:
                f.write("audio")
            return {"id": "track", "ext": "m4a"}

        def prepare_filename(self, info):
            return self.output_template.replace("%(id)s", info["id"]).replace(
                "%(ext)s", info["ext"])

    d = _tempfile.mkdtemp()
    original_download_dir = config.DOWNLOAD_DIR
    original_ydl = audio.yt_dlp.YoutubeDL
    original_cookie_file = config.COOKIE_FILE
    config.DOWNLOAD_DIR = d
    audio.yt_dlp.YoutubeDL = FakeYDL
    config.COOKIE_FILE = None
    try:
        result = audio.download_audio("https://example.com/video")
        path = result.path
        assert path is not None
        assert result.error is None
        assert os.path.dirname(os.path.dirname(path)) == d
        parent = os.path.dirname(path)
        past = _time.time() - 7200
        os.utime(parent, (past, past))
        removed = audio.cleanup_temp_files(max_age=3600)
        assert removed == 1
        assert not os.path.exists(parent)
    finally:
        config.DOWNLOAD_DIR = original_download_dir
        audio.yt_dlp.YoutubeDL = original_ydl
        config.COOKIE_FILE = original_cookie_file
        if os.path.isdir(d):
            for name in os.listdir(d):
                child = os.path.join(d, name)
                if os.path.isdir(child):
                    import shutil
                    shutil.rmtree(child)
                else:
                    os.remove(child)
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
    commands = MusicCog(bot).get_app_commands()
    names = {c.name for c in commands}
    expected = {
        "play", "skip", "queue", "loop", "shuffle", "speed", "pitch", "seek",
        "volume", "preset", "remove", "move", "clear", "join", "leave", "help",
        "stop", "pause", "resume", "nowplaying", "na-", "sound", "refresh",
        "playlist", "history", "historyplay", "previous", "replay",
        "favorite", "favorites",
        "playfavorite", "unfavorite", "settings", "stats", "playtop",
    }
    assert names == expected, (expected - names, names - expected)
    playlist = next(command for command in commands if command.name == "playlist")
    assert {command.name for command in playlist.commands} == {
        "add", "save", "load", "list", "delete",
    }


def test_initial_now_playing_message_waits_for_response():
    """The first /play followup must return the Message used by its updater."""
    import ast
    import inspect
    import textwrap

    from inmermusic.cog import MusicCog

    tree = ast.parse(textwrap.dedent(inspect.getsource(MusicCog._enqueue_songs)))
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


class FakeVoiceChannel:
    def __init__(self, humans=1, bots=1):
        self.members = (
            [type("M", (), {"bot": False})() for _ in range(humans)]
            + [type("M", (), {"bot": True})() for _ in range(bots)]
        )


def test_youtube_video_id():
    assert audio.youtube_video_id(
        "https://www.youtube.com/watch?v=abc123&list=RDabc123") == "abc123"
    assert audio.youtube_video_id("https://youtu.be/abc123") == "abc123"
    assert audio.youtube_video_id("https://www.youtube.com/shorts/abc123") == "abc123"
    assert audio.youtube_video_id("https://www.nicovideo.jp/watch/sm9") is None
    assert audio.youtube_video_id("https://www.youtube.com/playlist?list=PL1") is None
    assert audio.youtube_video_id(None) is None


def test_autoplay_refill_and_guards():
    """Autoplay refills a drained queue only for an occupied VC, and never
    spins when there is nothing left to suggest."""
    from inmermusic.state import get_state, guild_states

    def song(title, needs_local=False):
        return {"title": title, "url": f"https://www.youtube.com/watch?v={title}",
                "needs_local": needs_local}

    suggested = [song("rel1"), song("rel2")]
    disconnects = []
    lookups = []

    def fake_related_songs(url, guild_id=None, limit=5):
        lookups.append(url)
        return [dict(s) for s in suggested]

    def fake_load_history(guild_id, limit=20):
        return []

    def fake_make_audio_source(song, state, seek=0.0):
        return "SENTINEL_SOURCE"

    async def fake_announce_now_playing(gid):
        pass

    def fake_start_np_updater(gid, interval=None):
        pass

    async def fake_schedule_disconnect(gid):
        disconnects.append(gid)

    originals = {
        name: getattr(playback, name) for name in (
            "related_songs", "make_audio_source", "announce_now_playing",
            "start_np_updater", "schedule_disconnect")
    }
    original_load_history = playback.persistence.load_history
    playback.related_songs = fake_related_songs
    playback.persistence.load_history = fake_load_history
    playback.make_audio_source = fake_make_audio_source
    playback.announce_now_playing = fake_announce_now_playing
    playback.start_np_updater = fake_start_np_updater
    playback.schedule_disconnect = fake_schedule_disconnect

    guild_id = 900130
    try:
        # Autoplay off -> drained queue disconnects as before.
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = False
        asyncio.run(playback.play_next(guild_id))
        assert disconnects == [guild_id] and lookups == []

        # Autoplay on -> the seed's related tracks play instead.
        guild_states.pop(guild_id, None)
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = True
        state.autoplay_seed = song("seed")
        disconnects.clear()
        asyncio.run(playback.play_next(guild_id))
        assert lookups == [song("seed")["url"]]
        assert state.current_song["title"] == "rel1"
        assert state.current_song["autoplay"] is True
        assert state.current_song["requester"] == "オートDJ"
        assert [s["title"] for s in state.queue] == ["rel2"]
        assert disconnects == []
        assert state.autoplay_streak == 1

        # Nobody left in the VC -> no lookup, straight to idle disconnect.
        guild_states.pop(guild_id, None)
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel(humans=0)
        state.autoplay = True
        state.autoplay_seed = song("seed")
        lookups.clear()
        asyncio.run(playback.play_next(guild_id))
        assert lookups == [] and disconnects == [guild_id]

        # Streak cap reached -> stop refilling even with listeners present.
        guild_states.pop(guild_id, None)
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = True
        state.autoplay_seed = song("seed")
        state.autoplay_streak = config.AUTOPLAY_MAX_STREAK
        lookups.clear()
        disconnects.clear()
        asyncio.run(playback.play_next(guild_id))
        assert lookups == [] and disconnects == [guild_id]

        # No suggestions at all -> exactly one attempt, then disconnect.
        suggested.clear()
        guild_states.pop(guild_id, None)
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = True
        state.autoplay_seed = song("seed")
        lookups.clear()
        disconnects.clear()
        asyncio.run(playback.play_next(guild_id))
        assert len(lookups) == 1 and disconnects == [guild_id]

        # A user request resets the streak the next time it plays.
        guild_states.pop(guild_id, None)
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = True
        state.autoplay_streak = 7
        state.queue = [song("user-request")]
        asyncio.run(playback.play_next(guild_id))
        assert state.autoplay_streak == 0
        assert state.autoplay_seed["title"] == "user-request"
    finally:
        for name, value in originals.items():
            setattr(playback, name, value)
        playback.persistence.load_history = original_load_history
        guild_states.pop(guild_id, None)


def test_autoplay_falls_back_to_history():
    """A NicoNico seed yields no mix, so the guild's own history feeds the radio."""
    from inmermusic.state import get_state, guild_states

    history = [
        {"title": "old1", "url": "https://www.nicovideo.jp/watch/sm1"},
        {"title": "old2", "url": "https://www.nicovideo.jp/watch/sm2"},
        {"title": "queued", "url": "https://www.nicovideo.jp/watch/sm3"},
    ]

    def fake_related_songs(url, guild_id=None, limit=5):
        return []

    def fake_load_history(guild_id, limit=20):
        # The exclusion window asks for 30; the fallback pool asks for 200.
        return history if limit >= 200 else history[:1]

    original_related = playback.related_songs
    original_load_history = playback.persistence.load_history
    playback.related_songs = fake_related_songs
    playback.persistence.load_history = fake_load_history

    guild_id = 900131
    try:
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        state.voice_client.channel = FakeVoiceChannel()
        state.autoplay = True
        state.autoplay_seed = {
            "title": "seed", "url": "https://www.nicovideo.jp/watch/sm0"}
        state.queue = [history[2]]
        songs = asyncio.run(playback.collect_autoplay_songs(guild_id, state))
        titles = {song["title"] for song in songs}
        # old1 is in the recent window and "queued" is already queued.
        assert titles == {"old2"}
        assert songs[0]["autoplay"] is True
    finally:
        playback.related_songs = original_related
        playback.persistence.load_history = original_load_history
        guild_states.pop(guild_id, None)


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
    playback.download_audio = lambda url, guild_id=None: None  # failed download
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
    original_download_dir = config.DOWNLOAD_DIR
    original_ydl = audio.yt_dlp.YoutubeDL
    original_cookie_file = config.COOKIE_FILE
    config.DOWNLOAD_DIR = d
    audio.yt_dlp.YoutubeDL = FakeYDL
    config.COOKIE_FILE = None  # avoid touching the real cookie file path
    try:
        result = audio.download_audio("https://example.com/video")
        assert result.path is None
        assert result.error == "boom"
        leftovers = [n for n in os.listdir(d) if n.startswith("dl_")]
        assert leftovers == []
    finally:
        config.DOWNLOAD_DIR = original_download_dir
        audio.yt_dlp.YoutubeDL = original_ydl
        config.COOKIE_FILE = original_cookie_file
        os.rmdir(d)


def test_download_enforces_size_and_duration_limits():
    """Playlist entries dodge the length check; the download must catch it (#28)."""
    import tempfile as _tempfile

    # The hard stop yt-dlp applies before any bytes move.
    opts = audio.build_ydl_opts("https://example.com/v")
    assert opts["max_filesize"] == config.MAX_DOWNLOAD_BYTES

    class FakeYDL:
        info = {}

        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            return dict(FakeYDL.info)

        def prepare_filename(self, info):
            return os.path.join(config.DOWNLOAD_DIR, "never_written.m4a")

    d = _tempfile.mkdtemp()
    original_download_dir = config.DOWNLOAD_DIR
    original_ydl = audio.yt_dlp.YoutubeDL
    original_cookie_file = config.COOKIE_FILE
    config.DOWNLOAD_DIR = d
    audio.yt_dlp.YoutubeDL = FakeYDL
    config.COOKIE_FILE = None
    try:
        # A flat playlist entry has no duration up front, so it enters the
        # queue unchecked; the real value only shows up here.
        FakeYDL.info = {"duration": config.MAX_TRACK_DURATION + 1}
        result = audio.download_audio("https://example.com/long")
        assert result.path is None
        assert result.error == "動画が長すぎます"
        assert [n for n in os.listdir(d) if n.startswith("dl_")] == []

        # yt-dlp aborts an oversized download without raising, so no file is
        # written; report that as a size rejection, not a generic failure.
        FakeYDL.info = {"duration": 60,
                        "filesize": config.MAX_DOWNLOAD_BYTES + 1}
        result = audio.download_audio("https://example.com/huge")
        assert result.path is None
        assert result.error == "ファイルが大きすぎます"
        assert [n for n in os.listdir(d) if n.startswith("dl_")] == []

        # Within limits, the normal "file missing" path is unchanged.
        FakeYDL.info = {"duration": 60, "filesize": 1024}
        assert audio.download_audio("https://example.com/ok").error == \
            "downloaded file missing"
    finally:
        config.DOWNLOAD_DIR = original_download_dir
        audio.yt_dlp.YoutubeDL = original_ydl
        config.COOKIE_FILE = original_cookie_file
        os.rmdir(d)

    # Both rejections must reach the user as readable skip reasons.
    assert util.short_extract_error("動画が長すぎます") == "長すぎる動画"
    assert util.short_extract_error("ファイルが大きすぎます") == "ファイルが大きすぎる"
    assert "長すぎる" in util.friendly_extract_error("動画が長すぎます")


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

    def slow_download(url, guild_id=None):
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
    assert util.short_extract_error("Private video. Sign in") == "ログインが必要"
    assert util.short_extract_error("Connection timed out") == "ネットワークエラー"


def test_music_persistence_round_trip():
    import shutil
    import tempfile
    from inmermusic import persistence

    directory = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    config.STATE_DIR = directory
    song = {
        "url": "https://www.youtube.com/watch?v=persist",
        "title": "persist me",
        "duration": 123,
        "thumbnail": "https://example.com/x.jpg",
        "requester": "tester",
        "requester_id": 42,
        "needs_local": True,
        "local_file": "/tmp/secret-runtime-file",
        "audio_url": "https://signed.example/audio",
    }
    try:
        assert persistence.save_queue(7001, [song])
        restored = persistence.load_queue(7001)
        assert len(restored) == 1
        assert restored[0]["title"] == "persist me"
        assert restored[0]["local_file"] is None
        assert "audio_url" not in restored[0]

        assert persistence.record_history(7001, song)
        assert persistence.load_history(7001, 1)[0]["url"] == song["url"]
        assert persistence.pop_history(7001)["url"] == song["url"]
        assert persistence.load_history(7001) == []

        assert persistence.save_named_playlist(
            7001, "Favorites", [song], 42) == "saved"
        assert persistence.count_named_playlists(7001) == 1
        assert persistence.load_named_playlist(7001, "favorites")[0]["title"] == \
            "persist me"
        playlists = persistence.list_named_playlists(7001)
        assert playlists[0]["name"] == "Favorites"
        assert playlists[0]["song_count"] == 1
        assert persistence.delete_named_playlist(
            7001, "FAVORITES", 42) == "deleted"
        assert persistence.count_named_playlists(7001) == 0

        assert persistence.add_favorite(7001, 42, song)
        assert persistence.add_favorite(7001, 42, song)  # idempotent upsert
        assert len(persistence.load_favorites(7001, 42)) == 1
        assert persistence.remove_favorite(7001, 42, 1)["url"] == song["url"]

        settings = persistence.update_settings(
            7001, default_volume=140, idle_timeout=75, loop_mode="queue")
        assert settings == {
            "default_volume": 140, "idle_timeout": 75, "loop_mode": "queue",
            "autoplay": False}
        assert persistence.update_settings(7001, autoplay=True)["autoplay"] is True
        assert persistence.get_settings(7001)["autoplay"] is True
        # Turning it back off must persist: False is a value, not "unchanged".
        assert persistence.update_settings(7001, autoplay=False)["autoplay"] is False
        assert persistence.get_settings(7001) == {
            "default_volume": 140, "idle_timeout": 75, "loop_mode": "queue",
            "autoplay": False}
    finally:
        config.STATE_DIR = original_state_dir
        shutil.rmtree(directory)


def test_favorite_position_is_stable_across_limits():
    """/favorites and /unfavorite must number the same rows (issue #26)."""
    import shutil
    import tempfile
    from inmermusic import persistence

    directory = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    config.STATE_DIR = directory
    try:
        # Added back-to-back, so created_at ties at whole-second resolution.
        # Insert order is deliberately not url order.
        for index in [3, 0, 4, 1, 2]:
            assert persistence.add_favorite(7101, 42, {
                "url": f"https://www.youtube.com/watch?v=fav{index}",
                "title": f"favorite {index}",
                "duration": 60,
            })
        shown = persistence.load_favorites(7101, 42, config.FAVORITES_PAGE_SIZE)
        assert len(shown) == 5
        # Ties resolve by url, not by whatever order the query plan happens to
        # produce — otherwise position 1 can mean two different songs.
        assert [song["url"] for song in shown] == sorted(
            song["url"] for song in shown)
        # A different LIMIT may pick a different plan; the order must not depend
        # on it, since /favorites and /unfavorite resolve positions separately.
        assert [song["url"] for song in persistence.load_favorites(7101, 42, 200)] \
            == [song["url"] for song in shown]

        removed = persistence.remove_favorite(7101, 42, 1)
        assert removed["url"] == shown[0]["url"]
        assert [song["url"] for song in persistence.load_favorites(7101, 42)] \
            == [song["url"] for song in shown[1:]]
    finally:
        config.STATE_DIR = original_state_dir
        shutil.rmtree(directory)


def test_named_playlist_requires_owner_or_manage_guild():
    """Only the creator (or an admin) may overwrite/delete a playlist (#36)."""
    import shutil
    import tempfile
    from inmermusic import persistence

    def track(url):
        return {"url": url, "title": url, "duration": 60}

    owner, other = 111, 222
    directory = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    config.STATE_DIR = directory
    try:
        assert persistence.save_named_playlist(
            7102, "rock", [track("https://example.com/a")], owner) == "saved"

        # Another member can neither overwrite nor delete, and must not become
        # the owner by trying.
        assert persistence.save_named_playlist(
            7102, "rock", [track("https://example.com/b")], other) == "denied"
        assert persistence.delete_named_playlist(7102, "rock", other) == "denied"
        meta = persistence.get_named_playlist_meta(7102, "ROCK")
        assert meta["owner_id"] == owner
        assert persistence.load_named_playlist(7102, "rock")[0]["url"] == \
            "https://example.com/a"

        # The owner can overwrite; ownership stays put.
        assert persistence.save_named_playlist(
            7102, "rock", [track("https://example.com/c")], owner) == "saved"
        assert persistence.load_named_playlist(7102, "rock")[0]["url"] == \
            "https://example.com/c"
        assert persistence.get_named_playlist_meta(7102, "rock")["owner_id"] == owner

        # Manage Guild overrides, but still does not transfer ownership.
        assert persistence.save_named_playlist(
            7102, "rock", [track("https://example.com/d")], other,
            force=True) == "saved"
        assert persistence.get_named_playlist_meta(7102, "rock")["owner_id"] == owner
        assert persistence.load_named_playlist(7102, "rock")[0]["url"] == \
            "https://example.com/d"

        assert persistence.delete_named_playlist(7102, "nope", owner) == "missing"
        assert persistence.delete_named_playlist(
            7102, "rock", other, force=True) == "deleted"
        assert persistence.get_named_playlist_meta(7102, "rock") is None
        assert persistence.count_named_playlists(7102) == 0
    finally:
        config.STATE_DIR = original_state_dir
        shutil.rmtree(directory)


def test_play_count_aggregation():
    import shutil
    import tempfile
    from inmermusic import persistence

    def track(name, duration=60, requester_id=42):
        return {
            "url": f"https://www.youtube.com/watch?v={name}",
            "title": name,
            "duration": duration,
            "requester": "tester",
            "requester_id": requester_id,
        }

    directory = tempfile.mkdtemp()
    original_state_dir = config.STATE_DIR
    config.STATE_DIR = directory
    try:
        for _ in range(3):
            assert persistence.record_history(7101, track("alpha"))
        assert persistence.record_history(7101, track("beta", duration=30))
        # Another guild must not leak into 7101's totals.
        assert persistence.record_history(7102, track("alpha"))

        ranking = persistence.top_songs(7101)
        assert [entry["title"] for entry in ranking] == ["alpha", "beta"]
        assert ranking[0]["play_count"] == 3
        assert ranking[0]["total_sec"] == 180
        assert ranking[0]["song"]["url"] == track("alpha")["url"]
        assert persistence.top_songs(7102)[0]["play_count"] == 1
        assert persistence.guild_play_totals(7101) == {
            "unique_tracks": 2, "plays": 4, "total_sec": 210}

        # Ties break deterministically, so the ranking never reshuffles between
        # calls (the ordering bug class behind #26).
        for _ in range(2):
            assert persistence.record_history(7101, track("beta", duration=30))
        assert [entry["title"] for entry in persistence.top_songs(7101)] == \
            [entry["title"] for entry in persistence.top_songs(7101)]

        # history is trimmed to `limit` rows; the lifetime totals are not.
        for index in range(5):
            assert persistence.record_history(
                7101, track(f"filler{index}"), limit=2)
        assert len(persistence.load_history(7101, 50)) == 2
        assert persistence.top_songs(7101)[0]["play_count"] == 3
        assert persistence.guild_play_totals(7101)["plays"] == 11

        djs = persistence.top_requesters(7101)
        assert djs[0]["user_id"] == 42 and djs[0]["play_count"] == 11
        assert persistence.record_history(7101, track("gamma", requester_id=99))
        assert [entry["user_id"] for entry in persistence.top_requesters(7101)] == \
            [42, 99]
        assert persistence.user_play_stats(7101, 99)["play_count"] == 1
        assert persistence.user_play_stats(7101, 12345) == {
            "play_count": 0, "total_sec": 0, "songs": []}

        # A song without a URL can't be keyed, so it is counted in history only.
        before = persistence.guild_play_totals(7101)["plays"]
        assert persistence.record_history(7101, {"title": "no url"})
        assert persistence.guild_play_totals(7101)["plays"] == before

        persistence.delete_guild_data(7101)
        assert persistence.top_songs(7101) == []
        assert persistence.top_requesters(7101) == []
        assert persistence.guild_play_totals(7101)["plays"] == 0
        assert persistence.top_songs(7102)[0]["play_count"] == 1
    finally:
        config.STATE_DIR = original_state_dir
        shutil.rmtree(directory)


def test_proxy_failover_and_redaction():
    class FakeYDL:
        attempts = []

        def __init__(self, opts):
            self.opts = opts
            self.attempts.append(opts.get("proxy"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if len(self.attempts) == 1:
                raise RuntimeError("HTTP Error 429 via http://user:secret@one")
            return {
                "formats": [{
                    "acodec": "opus", "vcodec": "none",
                    "url": "https://audio.example/track.opus",
                }],
                "extractor_key": "Youtube",
                "webpage_url": url,
                "title": "fallback success",
                "duration": 60,
            }

    original_ydl = audio.yt_dlp.YoutubeDL
    original_proxies = config.YT_PROXIES
    original_proxy = config.YT_PROXY
    original_cookie = config.COOKIE_FILE
    original_preferred = audio._preferred_proxy
    config.YT_PROXIES = [
        "http://user:secret@one", "http://user:secret@two"]
    config.YT_PROXY = None
    config.COOKIE_FILE = None
    audio._preferred_proxy = None
    audio.yt_dlp.YoutubeDL = FakeYDL
    try:
        song = audio.extract_audio_url(
            "https://www.youtube.com/watch?v=proxy-test")
        assert song["title"] == "fallback success"
        assert FakeYDL.attempts == config.YT_PROXIES
        redacted = audio._redact_error(
            "failed http://user:secret@one and http://name:pass@example")
        assert "secret" not in redacted and "pass@" not in redacted
    finally:
        audio.yt_dlp.YoutubeDL = original_ydl
        config.YT_PROXIES = original_proxies
        config.YT_PROXY = original_proxy
        config.COOKIE_FILE = original_cookie
        audio._preferred_proxy = original_preferred


def test_search_and_playlist_flat_entries():
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {
                "entries": [
                    {
                        "id": "one", "title": "first", "duration": 60,
                        "extractor_key": "Youtube",
                    },
                    {
                        "id": "too-long", "title": "long",
                        "duration": config.MAX_TRACK_DURATION + 1,
                        "extractor_key": "Youtube",
                    },
                ]
            }

    original_ydl = audio.yt_dlp.YoutubeDL
    original_proxies = config.YT_PROXIES
    original_proxy = config.YT_PROXY
    audio.yt_dlp.YoutubeDL = FakeYDL
    config.YT_PROXIES = []
    config.YT_PROXY = None
    try:
        choices = audio.search_candidates("query", limit=5)
        assert choices[0]["url"] == "https://www.youtube.com/watch?v=one"
        songs = audio.extract_playlist(
            "https://www.youtube.com/playlist?list=test", limit=5)
        assert [song["title"] for song in songs] == ["first"]
    finally:
        audio.yt_dlp.YoutubeDL = original_ydl
        config.YT_PROXIES = original_proxies
        config.YT_PROXY = original_proxy


def test_failed_playback_never_reenters_loop():
    from inmermusic.state import get_state, guild_states

    guild_id = 900108
    original_play_next = playback.play_next

    async def fake_play_next(gid, announce=True):
        return None

    playback.play_next = fake_play_next
    try:
        state = get_state(guild_id)
        state.loop_mode = "song"
        state.current_song = {"title": "broken"}
        state.queue = []
        asyncio.run(playback.advance_queue(
            guild_id, state.current_song, expected_state=state, failed=True))
        assert state.queue == []
        assert state.current_song is None
    finally:
        playback.play_next = original_play_next
        guild_states.pop(guild_id, None)


def test_queue_embed_pagination_and_eta():
    from inmermusic.state import GuildState

    state = GuildState()
    state.queue = [
        {
            "title": f"song-{index}",
            "url": f"https://example.com/{index}",
            "duration": 60,
            "requester": "tester",
        }
        for index in range(12)
    ]
    embed = ui.create_queue_embed(
        state, page=1, page_size=10, current_remaining=30)
    assert "song-10" in embed.description
    assert "song-0" not in embed.description
    assert embed.footer.text.startswith("ページ 2/2")


def test_next_track_prefetch_is_bounded_and_reusable():
    import shutil
    import tempfile
    from inmermusic.state import get_state, guild_states

    guild_id = 900109
    root = tempfile.mkdtemp()
    made = os.path.join(root, "dl_prefetch")
    os.mkdir(made)
    path = os.path.join(made, "track.m4a")
    with open(path, "w") as handle:
        handle.write("audio")

    original_download = playback.download_audio
    playback.download_audio = lambda url, guild_id=None: path

    async def scenario():
        state = get_state(guild_id)
        song = {
            "title": "next", "url": "https://www.youtube.com/watch?v=next",
            "needs_local": True, "local_file": None,
        }
        state.queue = [song]
        playback.start_prefetch(guild_id)
        task = state.prefetch_task
        assert task is not None
        await task
        assert song["local_file"] == path
        assert state.prefetch_task is None

    try:
        asyncio.run(scenario())
    finally:
        playback.download_audio = original_download
        guild_states.pop(guild_id, None)
        shutil.rmtree(root, ignore_errors=True)


def test_stale_music_panel_is_rejected():
    from types import SimpleNamespace
    from inmermusic.state import get_state, guild_states

    guild_id = 900110
    replies = []

    class Response:
        async def send_message(self, message, **kwargs):
            replies.append((message, kwargs))

    async def scenario():
        state = get_state(guild_id)
        voice_client = object()
        state.voice_client = voice_client
        state.np_message = SimpleNamespace(id=100)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=guild_id, voice_client=voice_client),
            message=SimpleNamespace(id=99),
            response=Response(),
            user=SimpleNamespace(voice=None),
        )
        allowed = await ui.MusicControls().interaction_check(interaction)
        assert allowed is False
        assert "古く" in replies[0][0]
        assert replies[0][1]["ephemeral"] is True

    try:
        asyncio.run(scenario())
    finally:
        guild_states.pop(guild_id, None)


def test_stale_after_callback_keeps_new_playback(monkeypatch=None):
    """A late `after` callback must not clear the song that replaced it (#41)."""
    from inmermusic.state import get_state, guild_states

    class CapturingVoiceClient(FakeVoiceClient):
        def play(self, source, after=None):
            super().play(source, after=after)
            self.after = after

    guild_id = 900130
    recorded = []
    original_make = playback.make_audio_source
    original_announce = playback.announce_now_playing
    original_updater = playback.start_np_updater
    original_prefetch = playback.start_prefetch
    original_record = playback.persistence.record_history

    async def fake_announce(_guild_id):
        return None

    playback.make_audio_source = lambda song, state, seek=0.0: "SOURCE"
    playback.announce_now_playing = fake_announce
    playback.start_np_updater = lambda *args, **kwargs: None
    playback.start_prefetch = lambda *args, **kwargs: None
    playback.persistence.record_history = lambda gid, song: recorded.append(
        song["title"])

    async def scenario():
        state = get_state(guild_id)
        state.persistence_hydrated = True
        vc = CapturingVoiceClient()
        state.voice_client = vc
        old = {"title": "old", "needs_local": False}
        new = {"title": "new", "needs_local": False}

        state.queue = [old]
        await playback.play_next(guild_id)
        assert state.current_song is old
        stale_callback = vc.after

        # The old song ends, but its finish work is still queued on the loop
        # when a /play starts the next song on the (now idle) voice client.
        vc.stop()
        state.queue = [new]
        await playback.play_next(guild_id)
        assert state.current_song is new

        # Now the delayed callback lands. It must not touch the new playback.
        stale_callback(None)
        await asyncio.sleep(0.05)
        assert state.current_song is new
        assert state.queue == []
        # The finished song still counts as played, exactly once.
        assert recorded == ["old"]

    try:
        asyncio.run(scenario())
    finally:
        playback.make_audio_source = original_make
        playback.announce_now_playing = original_announce
        playback.start_np_updater = original_updater
        playback.start_prefetch = original_prefetch
        playback.persistence.record_history = original_record
        guild_states.pop(guild_id, None)


def test_previous_stops_current_song_before_responding():
    """/previous must not skip the song it just queued (#37)."""
    from types import SimpleNamespace
    from inmermusic import cog as cog_module
    from inmermusic.state import get_state, guild_states

    guild_id = 900131
    events = []

    class StoppingVoiceClient(FakeVoiceClient):
        def __init__(self):
            super().__init__()
            self._playing = True

        def stop(self):
            super().stop()
            events.append("stop")

    class Response:
        async def send_message(self, message, **kwargs):
            events.append("respond")

    original_persist = cog_module.persist_queue
    original_cancel = cog_module.cancel_prefetch
    original_start = cog_module.start_prefetch
    original_pop = cog_module.persistence.pop_history
    cog_module.persistence.pop_history = lambda gid: {
        "title": "prev", "url": "https://example.com/prev"}
    cog_module.persist_queue = lambda state: None
    cog_module.cancel_prefetch = lambda state: None
    cog_module.start_prefetch = lambda gid: events.append("prefetch")

    async def scenario():
        state = get_state(guild_id)
        vc = StoppingVoiceClient()
        state.voice_client = vc
        state.current_song = {"title": "now"}
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=guild_id, voice_client=vc),
            response=Response(),
        )
        cog = cog_module.MusicCog(bot=None)
        await cog.previous_cmd.callback(cog, interaction)
        assert state.queue[0]["title"] == "prev"
        assert state.skip_flag is True
        # The stop must be committed before the first await, so a song ending
        # during the Discord round-trip can't have the queued song stopped.
        assert events.index("stop") < events.index("respond")

    try:
        asyncio.run(scenario())
    finally:
        cog_module.persistence.pop_history = original_pop
        cog_module.persist_queue = original_persist
        cog_module.cancel_prefetch = original_cancel
        cog_module.start_prefetch = original_start
        guild_states.pop(guild_id, None)


def test_play_never_moves_a_busy_bot_to_another_vc():
    """A stale /play must not steal a session running in another VC (#35)."""
    from types import SimpleNamespace
    from inmermusic import cog as cog_module
    from inmermusic.state import guild_states

    guild_id = 900132
    sent = []
    channel_a = SimpleNamespace(name="VC-A")
    channel_b = SimpleNamespace(name="VC-B")

    class BusyVoiceClient(FakeVoiceClient):
        def __init__(self, channel):
            super().__init__()
            self.channel = channel
            self.moved_to = []

        async def move_to(self, channel):
            self.moved_to.append(channel)

    class Followup:
        async def send(self, message=None, **kwargs):
            sent.append(message)
            return SimpleNamespace(id=1)

    # Stubbed so that, without the fix, the run reaches the assertions below
    # instead of dying inside real playback.
    original_hydrate = cog_module.hydrate_state
    original_persist = cog_module.persist_queue
    original_play_next = cog_module.play_next
    original_prefetch = cog_module.start_prefetch

    async def fake_play_next(gid, announce=True):
        return None

    cog_module.hydrate_state = lambda gid: cog_module.get_state(gid)
    cog_module.persist_queue = lambda state: None
    cog_module.play_next = fake_play_next
    cog_module.start_prefetch = lambda gid: None

    async def scenario():
        vc = BusyVoiceClient(channel_b)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=guild_id, voice_client=vc),
            user=SimpleNamespace(
                id=5, display_name="A",
                voice=SimpleNamespace(channel=channel_a)),
            channel=SimpleNamespace(id=42),
            followup=Followup(),
        )
        cog = cog_module.MusicCog(bot=None)
        await cog._enqueue_songs(
            interaction, [{"title": "x", "url": "https://example.com/x"}])
        assert vc.moved_to == []
        assert sent and "別のVC" in sent[0]
        # Nothing was queued onto the other channel's session either.
        assert guild_id not in guild_states or not guild_states[guild_id].queue

        # Same channel: the request is allowed through to the normal path.
        interaction.user.voice.channel = channel_b
        assert cog_module.voice_conflict(interaction) is None

    try:
        asyncio.run(scenario())
    finally:
        cog_module.hydrate_state = original_hydrate
        cog_module.persist_queue = original_persist
        cog_module.play_next = original_play_next
        cog_module.start_prefetch = original_prefetch
        guild_states.pop(guild_id, None)


def test_sound_effect_interruption_keeps_downloaded_file():
    """A sound effect must not delete the file the song resumes from (#31)."""
    from inmermusic.state import get_state, guild_states

    class CapturingVoiceClient(FakeVoiceClient):
        def play(self, source, after=None):
            super().play(source, after=after)
            self.after = after

    guild_id = 900140
    removed = []
    original_make = playback.make_audio_source
    original_announce = playback.announce_now_playing
    original_updater = playback.start_np_updater
    original_prefetch = playback.start_prefetch
    original_cleanup = playback.cleanup_download

    async def fake_announce(_guild_id):
        return None

    playback.make_audio_source = lambda song, state, seek=0.0: "SOURCE"
    playback.announce_now_playing = fake_announce
    playback.start_np_updater = lambda *args, **kwargs: None
    playback.start_prefetch = lambda *args, **kwargs: None
    playback.cleanup_download = lambda path: removed.append(path)

    async def scenario():
        state = get_state(guild_id)
        vc = CapturingVoiceClient()
        state.voice_client = vc
        song = {"title": "s", "needs_local": True,
                "local_file": "/tmp/dl_x/a.m4a"}
        state.queue = [song]
        await playback.play_next(guild_id)
        after = vc.after

        # A sound effect stops the source deliberately; the file must survive
        # so restart_song can resume without re-downloading the whole track.
        state.is_playing_sound = True
        after(None)
        await asyncio.sleep(0.02)
        assert song["local_file"] == "/tmp/dl_x/a.m4a"
        assert removed == []

        # A genuine end-of-song still cleans up.
        state.is_playing_sound = False
        after(None)
        await asyncio.sleep(0.02)
        assert song["local_file"] is None
        assert removed == ["/tmp/dl_x/a.m4a"]

    try:
        asyncio.run(scenario())
    finally:
        playback.make_audio_source = original_make
        playback.announce_now_playing = original_announce
        playback.start_np_updater = original_updater
        playback.start_prefetch = original_prefetch
        playback.cleanup_download = original_cleanup
        guild_states.pop(guild_id, None)


def test_failed_play_start_cleans_up_its_download():
    """A vc.play() exception must not leak the popped song's dl_* dir (#33)."""
    from inmermusic.state import get_state, guild_states

    guild_id = 900141
    removed = []
    original_make = playback.make_audio_source
    original_notify = playback.notify_skip
    original_disconnect = playback.schedule_disconnect
    original_cleanup = playback.cleanup_download

    def exploding_source(song, state, seek=0.0):
        raise RuntimeError("boom")

    async def fake_notify(gid, song, reason, expected_state=None):
        return None

    async def fake_disconnect(gid):
        return None

    playback.make_audio_source = exploding_source
    playback.notify_skip = fake_notify
    playback.schedule_disconnect = fake_disconnect
    playback.cleanup_download = lambda path: path and removed.append(path)

    async def scenario():
        state = get_state(guild_id)
        state.voice_client = FakeVoiceClient()
        song = {"title": "boom", "needs_local": False,
                "local_file": "/tmp/dl_y/a.m4a"}
        state.queue = [song]
        await playback.play_next(guild_id)
        assert removed == ["/tmp/dl_y/a.m4a"]
        assert song["local_file"] is None

    try:
        asyncio.run(scenario())
    finally:
        playback.make_audio_source = original_make
        playback.notify_skip = original_notify
        playback.schedule_disconnect = original_disconnect
        playback.cleanup_download = original_cleanup
        guild_states.pop(guild_id, None)


def test_temp_sweep_spares_files_still_in_use():
    """The periodic sweep must not delete a long track being played (#33)."""
    import shutil
    import tempfile
    from inmermusic.state import get_state, guild_states

    guild_id = 900142
    directory = tempfile.mkdtemp()
    original_dir = config.DOWNLOAD_DIR
    config.DOWNLOAD_DIR = directory
    try:
        in_use_dir = os.path.join(directory, "dl_inuse")
        stale_dir = os.path.join(directory, "dl_stale")
        os.makedirs(in_use_dir)
        os.makedirs(stale_dir)
        in_use_file = os.path.join(in_use_dir, "a.m4a")
        open(in_use_file, "w").close()
        # Both look old enough to sweep: a track longer than max_age has an
        # old mtime too, which is exactly the dangerous case.
        old = time.time() - 7200
        for path in (in_use_dir, stale_dir):
            os.utime(path, (old, old))

        state = get_state(guild_id)
        state.current_song = {"title": "long", "local_file": in_use_file}

        removed = audio.cleanup_temp_files(
            max_age=3600, in_use=playback.active_download_paths())
        assert removed == 1
        assert os.path.isdir(in_use_dir)
        assert not os.path.exists(stale_dir)

        # Without the protection set it is swept like anything else.
        assert audio.cleanup_temp_files(max_age=3600) == 1
        assert not os.path.exists(in_use_dir)
    finally:
        config.DOWNLOAD_DIR = original_dir
        shutil.rmtree(directory, ignore_errors=True)
        guild_states.pop(guild_id, None)


def test_startup_work_runs_once_per_process():
    """on_ready refires on every reconnect; tree.sync must not (#25)."""
    from types import SimpleNamespace
    from inmermusic import bot as bot_module

    syncs = []
    tasks = []

    class FakeTree:
        async def sync(self):
            syncs.append(1)
            return []

    class FakeLoop:
        def create_task(self, coro):
            coro.close()  # never scheduled; we only care that it was requested
            tasks.append(1)

    original_bot = bot_module.bot
    original_cleanup = bot_module.cleanup_temp_files
    sweeps = []
    bot_module.cleanup_temp_files = lambda *a, **k: sweeps.append(1)
    bot_module.bot = SimpleNamespace(
        user="fake", tree=FakeTree(), loop=FakeLoop())

    async def scenario():
        await bot_module.on_ready()
        # A gateway reconnect fires on_ready again.
        await bot_module.on_ready()
        await bot_module.on_ready()
        assert syncs == [1], syncs
        assert sweeps == [1], sweeps
        # Both background loops start, and only once.
        assert len(tasks) == 2, tasks

    try:
        asyncio.run(scenario())
    finally:
        bot_module.bot = original_bot
        bot_module.cleanup_temp_files = original_cleanup


def test_panel_refresh_is_debounced():
    """Button mashing must coalesce into one message.edit (#29)."""
    from types import SimpleNamespace
    from inmermusic.state import get_state, guild_states

    guild_id = 900150
    edits = []

    class Message:
        id = 7

        async def edit(self, **kwargs):
            edits.append(kwargs)

    class PlayingVoiceClient(FakeVoiceClient):
        def __init__(self):
            super().__init__()
            self._playing = True

    async def scenario():
        state = get_state(guild_id)
        state.voice_client = PlayingVoiceClient()
        state.current_song = {"title": "t", "url": "https://example.com/t",
                              "duration": 100}
        state.np_message = Message()

        for _ in range(5):
            playback.schedule_refresh_now_playing(guild_id)
            await asyncio.sleep(0.01)
        assert edits == []  # nothing fired during the burst
        await asyncio.sleep(config.EFFECT_DEBOUNCE + 0.3)
        assert len(edits) == 1, edits

        # The pending task must not outlive the guild (the reapply_task leak).
        playback.schedule_refresh_now_playing(guild_id)
        task = state.np_refresh_task
        playback.cleanup_guild_state(guild_id)
        await asyncio.sleep(0.01)
        assert task.cancelled() or task.done()
        assert guild_id not in guild_states

    original_persist = playback.persist_queue
    playback.persist_queue = lambda state: None
    try:
        asyncio.run(scenario())
    finally:
        playback.persist_queue = original_persist
        guild_states.pop(guild_id, None)
    assert isinstance(SimpleNamespace(), object)


def test_np_updater_goes_quiet_while_paused():
    """A paused song must not be re-rendered every interval forever (#32)."""
    from inmermusic.state import get_state, guild_states

    guild_id = 900151
    edits = []

    class Message:
        id = 8

        async def edit(self, **kwargs):
            edits.append(kwargs)

    class PausableVoiceClient(FakeVoiceClient):
        def __init__(self):
            super().__init__()
            self._playing = True
            self._paused = False

        def is_playing(self):
            return self._playing and not self._paused

        def is_paused(self):
            return self._paused

    async def scenario():
        state = get_state(guild_id)
        vc = PausableVoiceClient()
        state.voice_client = vc
        state.current_song = {"title": "t", "url": "https://example.com/t",
                              "duration": 100}
        state.np_message = Message()

        vc._paused = True
        state.clock_paused = True
        playback.start_np_updater(guild_id, interval=0.01)
        await asyncio.sleep(0.1)
        assert edits == [], edits  # ~10 intervals, zero identical edits

        # Resuming picks the progress bar back up without a restart.
        vc._paused = False
        state.clock_paused = False
        await asyncio.sleep(0.05)
        assert edits, "updater did not resume after unpause"
        playback.cancel_np_updater(state)

    try:
        asyncio.run(scenario())
    finally:
        guild_states.pop(guild_id, None)


def test_paused_embed_says_so():
    """The panel has to explain why the progress bar stopped (#32)."""
    from inmermusic.state import GuildState

    state = GuildState(1)
    song = {"title": "t", "url": "https://example.com/t", "duration": 100}
    assert ui.create_now_playing_embed(song, state=state).title == "再生中"
    state.clock_paused = True
    assert "一時停止" in ui.create_now_playing_embed(song, state=state).title


def test_idle_guild_state_is_released():
    """Browsing commands must not register a guild forever (#30)."""
    from inmermusic.state import (drop_if_idle, get_state, guild_states,
                                  is_idle)

    guild_id = 900152
    try:
        state = get_state(guild_id)
        assert is_idle(state)
        assert drop_if_idle(guild_id) is True
        assert guild_id not in guild_states

        # A guild holding anything real is kept.
        for setup in (
            lambda s: setattr(s, "voice_client", object()),
            lambda s: setattr(s, "current_song", {"title": "t"}),
            lambda s: s.queue.append({"title": "t"}),
            lambda s: setattr(s, "np_message", object()),
            lambda s: setattr(s, "dispatching", True),
            # Runtime-only knobs would be silently lost if we dropped these.
            lambda s: setattr(s, "speed", 1.5),
            lambda s: setattr(s, "pitch", 3),
            lambda s: setattr(s, "effect", "bassboost"),
            lambda s: setattr(s, "volume", 50),
        ):
            state = get_state(guild_id)
            setup(state)
            assert drop_if_idle(guild_id) is False, setup
            assert guild_states.get(guild_id) is state
            guild_states.pop(guild_id, None)

        # volume matching the guild default is not a user tweak.
        state = get_state(guild_id)
        state.volume = state.default_volume = 70
        assert drop_if_idle(guild_id) is True
    finally:
        guild_states.pop(guild_id, None)


def test_settings_reschedules_a_running_idle_timer():
    """A sleeping disconnect timer must pick up a new idle_timeout (#32)."""
    from inmermusic.state import get_state, guild_states

    guild_id = 900153

    async def scenario():
        state = get_state(guild_id)
        state.idle_timeout = 3600
        state.idle_task = asyncio.create_task(
            playback.schedule_disconnect(guild_id))
        await asyncio.sleep(0)
        first = state.idle_task

        # Emulate the /settings branch: shorten the timeout and restart.
        state.idle_timeout = 30
        playback.cancel_idle_task(guild_id)
        state.idle_task = asyncio.create_task(
            playback.schedule_disconnect(guild_id))
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        assert state.idle_task is not first
        playback.cancel_idle_task(guild_id)

    try:
        asyncio.run(scenario())
    finally:
        guild_states.pop(guild_id, None)


def test_nico_cli_never_prints_session_secret():
    import contextlib
    import io
    import shutil
    import tempfile
    from inmermusic import nico_cli

    directory = tempfile.mkdtemp()
    session_file = os.path.join(directory, "session.txt")
    secret = "user_session_sensitive_value"
    with open(session_file, "w") as handle:
        handle.write(secret)
    original_state_dir = config.STATE_DIR
    config.STATE_DIR = directory
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            assert nico_cli.main(
                ["set", "8001", "--session-file", session_file]) == 0
            assert nico_cli.main(["status", "8001"]) == 0
            assert nico_cli.main(["list"]) == 0
        assert secret not in output.getvalue()
        assert "8001" in output.getvalue()
    finally:
        config.STATE_DIR = original_state_dir
        shutil.rmtree(directory)


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
