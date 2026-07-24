"""Local-only administration for per-guild niconico sessions."""
import argparse
import getpass
import sys
import time

from .cookies import (delete_guild_session, get_guild_session,
                      list_guild_sessions, set_guild_session)


def _read_session(path: str | None) -> str:
    if path:
        with open(path) as handle:
            value = handle.read().strip()
    else:
        value = getpass.getpass("niconico user_session: ").strip()
    if len(value) < 8 or any(char.isspace() for char in value):
        raise ValueError("session value is empty or malformed")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage per-guild niconico sessions without sending secrets to Discord.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    set_parser = subparsers.add_parser("set", help="store or replace a guild session")
    set_parser.add_argument("guild_id", type=int)
    set_parser.add_argument(
        "--session-file",
        help="read the session from a local file instead of a hidden prompt")
    delete_parser = subparsers.add_parser("delete", help="delete a guild session")
    delete_parser.add_argument("guild_id", type=int)
    status_parser = subparsers.add_parser("status", help="show whether a guild is configured")
    status_parser.add_argument("guild_id", type=int)
    subparsers.add_parser("list", help="list configured guild IDs without secrets")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "set":
        try:
            session = _read_session(args.session_file)
            set_guild_session(args.guild_id, session)
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"configured guild {args.guild_id}")
        return 0
    if args.command == "delete":
        delete_guild_session(args.guild_id)
        print(f"deleted guild {args.guild_id}")
        return 0
    if args.command == "status":
        configured = get_guild_session(args.guild_id) is not None
        print(f"guild {args.guild_id}: {'configured' if configured else 'not configured'}")
        return 0 if configured else 1
    for row in list_guild_sessions():
        updated = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(row["updated_at"]))
        print(f"{row['guild_id']}\t{updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
