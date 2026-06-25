"""Logic tests for main.py (no Discord connection required).

Run either way:
    python tests/test_features.py     # standalone, no extra deps
    pytest tests/test_features.py     # if pytest is installed

Importing main.py is safe: bot.run() is guarded by __name__ == "__main__",
so importing it here does not start the bot.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main


def test_build_audio_filter_defaults():
    # All defaults -> no filter (lossless passthrough)
    assert main.build_audio_filter(1.0, 0, 100, "off") is None


def test_build_audio_filter_volume():
    assert "volume=2.000" in main.build_audio_filter(1.0, 0, 200, "off")
    assert "volume=0.500" in main.build_audio_filter(1.0, 0, 50, "off")


def test_build_audio_filter_effects():
    assert "bass=g=12" in main.build_audio_filter(1.0, 0, 100, "bassboost")
    assert "apulsator" in main.build_audio_filter(1.0, 0, 100, "8d")
    assert "lowpass" in main.build_audio_filter(1.0, 0, 100, "lofi")


def test_build_audio_filter_speed_pitch():
    assert "atempo" in main.build_audio_filter(1.5, 0, 100, "off")
    af = main.build_audio_filter(1.0, 3, 100, "off")
    # pitch via asetrate shifts speed; atempo compensates back
    assert "asetrate" in af and "atempo" in af


def test_build_audio_filter_combo():
    af = main.build_audio_filter(1.25, 3, 150, "bassboost")
    for token in ("asetrate", "atempo", "bass=g=12", "volume=1.500"):
        assert token in af, token


def test_parse_time():
    assert main.parse_time("90") == 90
    assert main.parse_time("1:30") == 90
    assert main.parse_time("1:02:03") == 3723
    assert main.parse_time("0:05") == 5
    assert main.parse_time("abc") is None
    assert main.parse_time("") is None
    assert main.parse_time("1:2:3:4") is None


def test_fmt_duration():
    assert main.fmt_duration(90) == "1:30"
    assert main.fmt_duration(3723) == "1:02:03"
    assert main.fmt_duration(5) == "0:05"
    assert main.fmt_duration(-10) == "0:00"


def test_make_progress_bar():
    bar = main.make_progress_bar(90, 180)
    assert "1:30" in bar and "3:00" in bar
    assert "\U0001f518" in bar              # position marker present
    assert main.make_progress_bar(10, 0) == ""   # unknown duration -> empty
    assert main.make_progress_bar(9999, 180) != ""  # clamps over 100%


def test_write_netscape_cookies():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    original = main.COOKIE_FILE
    try:
        main.COOKIE_FILE = path
        count = main.write_netscape_cookies([
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
        main.COOKIE_FILE = original
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
    original = main.tempfile.gettempdir
    try:
        main.tempfile.gettempdir = lambda: d
        removed = main.cleanup_temp_files(max_age=3600)
    finally:
        main.tempfile.gettempdir = original
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
    assert set(main.EFFECT_PRESETS) == set(main.EFFECT_LABELS)
    # Every preset references a defined effect filter and carries a full spec.
    for key, preset in main.EFFECT_PRESETS.items():
        assert preset["effect"] in main.EFFECT_FILTERS, key
        assert {"speed", "pitch", "effect"} <= set(preset), key


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
