"""Focused regressions for flat search and playlist discovery."""

from inmermusic import audio, config


def test_flat_discovery_skips_entries_without_concrete_urls(monkeypatch):
    captured = []

    def fake_extract(url, guild_id, **options):
        captured.append((url, options))
        return {
            "entries": [
                {"title": "missing everything"},
                {
                    "id": "youtube-id",
                    "title": "YouTube ID only",
                    "extractor_key": "Youtube",
                    "duration": 10,
                },
            ]
        }

    monkeypatch.setattr(audio, "_extract_info_with_failover", fake_extract)

    choices = audio.search_candidates("query", limit=100)
    assert [song["title"] for song in choices] == ["YouTube ID only"]
    assert choices[0]["url"] == \
        "https://www.youtube.com/watch?v=youtube-id"
    assert captured[0][0] == "ytsearch25:query"

    songs = audio.extract_playlist(
        "https://www.youtube.com/playlist?list=PL1", limit=10)
    assert [song["title"] for song in songs] == ["YouTube ID only"]
    assert all(song["url"] != "https://www.youtube.com/playlist?list=PL1"
               for song in songs)


def test_search_candidate_keeps_uploader_metadata(monkeypatch):
    monkeypatch.setattr(
        audio,
        "_extract_info_with_failover",
        lambda *args, **kwargs: {
            "entries": [{
                "id": "one",
                "title": "Track",
                "channel": "Channel name",
                "extractor_key": "Youtube",
                "duration": 60,
            }]
        },
    )

    assert audio.search_candidates("query")[0]["uploader"] == "Channel name"


def test_niconico_playlist_refreshes_only_without_guild_session(monkeypatch):
    refreshes = []
    captured = []
    entries = [{
        "id": "sm9",
        "title": "Nico",
        "extractor_key": "Niconico",
        "duration": 10,
    }]

    monkeypatch.setattr(
        audio, "refresh_nico_cookies_sync",
        lambda: refreshes.append(True))
    monkeypatch.setattr(
        audio, "_extract_info_with_failover",
        lambda *args, **kwargs: captured.append(kwargs) or {"entries": entries})

    monkeypatch.setattr(audio, "guild_cookie_file", lambda guild_id: None)
    songs = audio.extract_playlist(
        "https://www.nicovideo.jp/mylist/1", guild_id=42)
    assert refreshes == [True]
    assert songs[0]["url"] == "https://www.nicovideo.jp/watch/sm9"
    assert captured[-1]["_guild_cookie"] is None

    monkeypatch.setattr(
        audio, "guild_cookie_file",
        lambda guild_id: "/secure/guild-cookie.txt")
    audio.extract_playlist(
        "https://www.nicovideo.jp/mylist/1", guild_id=42)
    assert refreshes == [True]
    assert captured[-1]["_guild_cookie"] == "/secure/guild-cookie.txt"


def test_playlist_limit_remains_bounded_by_configuration(monkeypatch):
    captured = {}

    def fake_extract(url, guild_id, **options):
        captured.update(options)
        return {"entries": []}

    monkeypatch.setattr(audio, "_extract_info_with_failover", fake_extract)
    audio.extract_playlist(
        "https://www.youtube.com/playlist?list=PL1",
        limit=config.MAX_PLAYLIST_SIZE + 100,
    )
    assert captured["playlistend"] == config.MAX_PLAYLIST_SIZE
