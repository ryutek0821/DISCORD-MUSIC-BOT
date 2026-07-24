"""Operational error handling for the local niconico session CLI."""

import sqlite3

import pytest

from inmermusic import nico_cli


def test_delete_does_not_claim_success_when_guild_is_missing(
    monkeypatch, capsys,
):
    monkeypatch.setattr(
        nico_cli, "delete_guild_session",
        lambda guild_id, suppress_errors=False: False)

    assert nico_cli.main(["delete", "123"]) == 1
    output = capsys.readouterr()
    assert "deleted" not in output.out
    assert "not configured" in output.err


def test_status_reports_store_failure(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(nico_cli, "get_guild_session", fail)
    assert nico_cli.main(["status", "123"]) == 1
    assert "database unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["0", "-1", str(2**63), "not-a-number"])
def test_cli_rejects_invalid_guild_ids(value):
    with pytest.raises(SystemExit):
        nico_cli.build_parser().parse_args(["status", value])
