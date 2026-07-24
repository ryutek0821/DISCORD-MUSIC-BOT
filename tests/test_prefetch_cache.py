"""Focused regressions for the one-track-per-guild prefetch cache."""

import asyncio
from pathlib import Path

from inmermusic import audio, playback
from inmermusic.state import get_state, guild_states


def _download_file(root: Path, name: str, content: bytes = b"audio") -> str:
    directory = root / f"dl_{name}"
    directory.mkdir()
    path = directory / "track.m4a"
    path.write_bytes(content)
    return str(path)


def test_retargeted_prefetch_discards_completed_old_head(
    monkeypatch, tmp_path,
):
    guild_id = 910001
    old_path = _download_file(tmp_path, "old")
    new_path = _download_file(tmp_path, "new")

    async def scenario():
        state = get_state(guild_id)
        old_head = {
            "title": "old",
            "url": "https://www.youtube.com/watch?v=old",
            "needs_local": True,
            "local_file": old_path,
        }
        new_head = {
            "title": "new",
            "url": "https://www.youtube.com/watch?v=new",
            "needs_local": True,
            "local_file": None,
        }
        state.queue = [new_head, old_head]
        monkeypatch.setattr(
            playback, "download_audio",
            lambda *args, **kwargs: audio.DownloadResult(new_path))

        playback.start_prefetch(guild_id)
        task = state.prefetch_task
        assert task is not None
        await task

        assert old_head["local_file"] is None
        assert not Path(old_path).exists()
        assert new_head["local_file"] == new_path

    try:
        asyncio.run(scenario())
    finally:
        guild_states.pop(guild_id, None)


def test_oversized_prefetch_is_removed(monkeypatch, tmp_path):
    guild_id = 910002
    path = _download_file(tmp_path, "large", b"too-large")

    async def scenario():
        state = get_state(guild_id)
        song = {
            "title": "large",
            "url": "https://www.youtube.com/watch?v=large",
            "needs_local": True,
            "local_file": None,
        }
        state.queue = [song]
        monkeypatch.setattr(playback, "PREFETCH_MAX_BYTES", 3)
        monkeypatch.setattr(
            playback, "download_audio",
            lambda *args, **kwargs: audio.DownloadResult(path))

        playback.start_prefetch(guild_id)
        task = state.prefetch_task
        assert task is not None
        await task

        assert song["local_file"] is None
        assert not Path(path).exists()

    try:
        asyncio.run(scenario())
    finally:
        guild_states.pop(guild_id, None)
