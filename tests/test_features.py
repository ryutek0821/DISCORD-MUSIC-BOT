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


def test_preset_integrity():
    assert set(main.EFFECT_PRESETS) == {"off", "nightcore", "vaporwave", "bassboost", "8d", "lofi"}
    assert all(v["effect"] in main.EFFECT_FILTERS for v in main.EFFECT_PRESETS.values())


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
