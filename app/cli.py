"""Command line entry: python -m app.cli <auth|whoami|playlists|channels|run|preview>."""
import argparse
import sys
from datetime import datetime, timezone

from app import auth
from app import run as runner
from app.config import settings
from app.graph import GraphError, NotImplementedInThisVersion, load_graph
from app.state import State
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


def _graph(path: str):
    try:
        return load_graph(path)
    except GraphError as err:
        print("Graph is invalid:", *(f"  {e}" for e in err.errors), sep="\n", file=sys.stderr)
        return None


def cmd_run(args: argparse.Namespace) -> int:
    graph = _graph(args.graph)
    if graph is None:
        return 1
    yt, meter = _client()
    state = State.throwaway_copy() if args.dry_run else State()
    try:
        report = runner.run(
            graph, yt, state, meter, datetime.now(timezone.utc), settings.daily_quota_budget,
            allow_any_playlist=settings.allow_any_playlist, dry_run=args.dry_run, backfill=args.backfill, fetch=runner.fetch_url,
        )
    finally:
        state.close()
    print(runner.render(report))
    return 1 if report.config_errors else 0


def cmd_preview(args: argparse.Namespace) -> int:
    graph = _graph(args.graph)
    if graph is None:
        return 1
    if args.node not in graph.nodes:
        print(f"Error: node {args.node!r} is not in the graph", file=sys.stderr)
        return 1
    yt, meter = _client()
    state = State.throwaway_copy()
    try:
        passing = runner.preview_node(graph, args.node, yt, state, datetime.now(timezone.utc), runner.fetch_url, args.backfill)
    finally:
        state.close()
    print(f"{len(passing)} recent video(s) pass node {args.node}:")
    for v in passing:
        print(f"  {v.video_id}  {v.title}")
    print(f"Read-only. Quota used by this preview: {meter.total} units (RSS is free).")
    return 0


COMMANDS = {
    "auth": cmd_auth, "whoami": cmd_whoami, "playlists": cmd_playlists, "channels": cmd_channels,
    "run": cmd_run, "preview": cmd_preview,
}
AUTH_EXPIRED = (
    "Google sign-in expired or was revoked - run: py -3.12 -m uv run python -m app.cli auth "
    "(an app left in Testing mode expires every 7 days)"
)


def _safe_console() -> None:
    # video and playlist titles carry emoji; a cp1252 console (or a redirected log) cannot encode them
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--graph", help="graph file (run, preview)")
    parser.add_argument("--dry-run", action="store_true", help="run: read-only preview, writes nothing")
    parser.add_argument("--node", help="preview: node id to test")
    parser.add_argument("--backfill", type=int, default=None, help="newest videos to take from a channel on its first run")
    args = parser.parse_args(argv)
    if args.command in ("run", "preview") and not args.graph:
        parser.error("--graph is required")
    if args.command == "preview" and not args.node:
        parser.error("--node is required")
    if args.backfill is None:
        args.backfill = 10 if args.command == "preview" else 0
    try:
        return COMMANDS[args.command](args)
    except auth.AuthExpiredError:
        print(AUTH_EXPIRED, file=sys.stderr)
        return 2
    except NotImplementedInThisVersion as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except (auth.AuthError, YouTubeError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
