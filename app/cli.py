"""Command line entry: python -m app.cli <auth|whoami|playlists|channels>."""
import argparse
import sys

from app import auth
from app.youtube import QuotaMeter, RealYouTube, YouTubeError


def _client() -> tuple[RealYouTube, QuotaMeter]:
    meter = QuotaMeter()
    return RealYouTube.from_credentials(auth.load_credentials(), meter), meter


def cmd_auth(_: argparse.Namespace) -> int:
    auth.run_auth()
    print("Signed in. Refresh token stored in the system keychain.")
    return 0


def cmd_whoami(_: argparse.Namespace) -> int:
    yt, meter = _client()
    ch = yt.my_channel()
    print(f"{ch['snippet']['title']} ({ch['id']})")
    print(f"Quota used: {meter.total} units")
    return 0


def cmd_playlists(_: argparse.Namespace) -> int:
    yt, _meter = _client()
    for p in yt.my_playlists():
        print(f"{p['id']}  {p['snippet']['title']}")
    return 0


def cmd_channels(_: argparse.Namespace) -> int:
    yt, _meter = _client()
    for s in yt.subscriptions():
        print(f"{s['snippet']['resourceId']['channelId']}  {s['snippet']['title']}")
    return 0


COMMANDS = {"auth": cmd_auth, "whoami": cmd_whoami, "playlists": cmd_playlists, "channels": cmd_channels}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=COMMANDS)
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (auth.AuthError, YouTubeError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
