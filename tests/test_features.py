"""Logic tests for the inmermusic package (no Discord connection required).

Run either way:
    python tests/test_features.py     # standalone, no extra deps
    pytest tests/test_features.py     # if pytest is installed

Importing the package is safe: bot.run() is only called from main.py under
__name__ == "__main__", so importing here never starts the bot.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inmermusic import audio, config, cookies, ui, util


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


def test_is_playlist_url():
    playlists = [
        "https://www.youtube.com/playlist?list=PLabc",
        "https://music.youtube.com/playlist?list=OLAK5",
        "https://www.nicovideo.jp/user/123/mylist/456",
        "https://www.nicovideo.jp/series/789",
    ]
    singles = [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://www.youtube.com/watch?v=abc&list=RDxyz",  # single video inside a mix
        "https://www.nicovideo.jp/watch/sm123",
        "never gonna give you up",
        "best playlist 2020",
    ]
    for u in playlists:
        assert audio.is_playlist_url(u) is True, u
    for u in singles:
        assert audio.is_playlist_url(u) is False, u


def test_playlist_entry_to_song():
    from inmermusic.audio import _playlist_entry_to_song
    # Entry carrying a URL.
    s = _playlist_entry_to_song(
        {"url": "https://youtu.be/x", "title": "T", "duration": 100, "thumbnail": "th"}, False)
    assert s["url"] == "https://youtu.be/x"
    assert (s["title"], s["duration"], s["thumbnail"]) == ("T", 100, "th")
    assert s["needs_local"] is True and s["audio_url"] is None and s["local_file"] is None
    # Bare id -> constructed watch URL.
    assert _playlist_entry_to_song({"id": "abc123"}, False)["url"] == \
        "https://www.youtube.com/watch?v=abc123"
    # niconico flag preserved; missing title defaults.
    s3 = _playlist_entry_to_song({"url": "https://nicovideo.jp/watch/sm1"}, True)
    assert s3["is_niconico"] is True and s3["title"] == "Unknown"
    # No url and no id -> skipped.
    assert _playlist_entry_to_song({"title": "no url"}, False) is None


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
