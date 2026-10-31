"""The daily run: detect new uploads, run them through the graph, insert matches into playlists within the quota budget.

Only playlistItems.insert writes to YouTube. A video is marked seen once all its filings were attempted; videos cut
short by the budget, a rate limit or a quota error stay unseen and are picked up by the next run. A dry run reads
from a throwaway copy of the state and never inserts.
"""
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from app.detect import Detection, Video, detect_new
from app.graph import Graph, RunResult, execute, required_channels
from app.rss import Fetcher, fetch_url
from app.state import State
from app.youtube import (
    INSERT_COST,
    AlreadyInPlaylistError,
    PlaylistFullError,
    QuotaExceededError,
    QuotaMeter,
    RateLimitedError,
    YouTubeAPI,
    YouTubeError,
)

QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # the daily quota resets at midnight Pacific
SUBS_CACHE_KEY = "subscriptions"
SUBS_CACHE_TTL = timedelta(hours=24)
MAX_FAILED_RUNS = 3
RATE_LIMIT_PAUSE_SECONDS = 2
PLAYLIST_LIMIT = 5000


def quota_day(now: datetime) -> str:
    return now.astimezone(QUOTA_TZ).date().isoformat()


@dataclass
class Report:
    dry_run: bool = False
    new_videos: int = 0
    filed: Counter = field(default_factory=Counter)  # playlist id -> inserted
    already: int = 0  # filings skipped because they were already recorded or already on YouTube
    deferred: int = 0  # filings not attempted (budget, rate limit, quota); their videos stay unseen
    dropped_by_node: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    config_errors: list[str] = field(default_factory=list)
    gave_up: list[str] = field(default_factory=list)
    full_playlists: set[str] = field(default_factory=set)
    quota_stopped: bool = False
    units_run: int = 0  # units this run spent (dry run: on detection reads)
    units_projected: int = 0  # dry run: units a real run would spend, inserts included
    units_today: int = 0
    budget: int = 0
    titles: dict[str, str] = field(default_factory=dict)  # playlist id -> title
    # dry run only
    result: RunResult | None = None
    video_titles: dict[str, str] = field(default_factory=dict)
    node_labels: dict[str, str] = field(default_factory=dict)
    projected: Counter = field(default_factory=Counter)
    fallback_channels: int = 0


def _destinations(graph: Graph) -> dict[str, str | None]:
    return {
        n.params.playlist_id: n.params.title for n in graph.nodes.values() if n.type == "destination.playlist"
    }


def resolve_subscriptions(yt: YouTubeAPI, state: State, now: datetime) -> set[str]:
    """Subscribed channel ids, cached in state for 24 hours to save quota."""
    cached = state.kv_get(SUBS_CACHE_KEY)
    if cached and now - cached[1] < SUBS_CACHE_TTL:
        return set(json.loads(cached[0]))
    ids = sorted({s["snippet"]["resourceId"]["channelId"] for s in yt.subscriptions()})
    state.kv_set(SUBS_CACHE_KEY, json.dumps(ids), now)
    return set(ids)


def detect_recent(
    graph: Graph, yt: YouTubeAPI, state: State, now: datetime, fetch: Fetcher, backfill: int
) -> tuple[Detection, set[str] | None]:
    subs: set[str] | None = None

    def resolver() -> set[str]:
        nonlocal subs
        subs = resolve_subscriptions(yt, state, now)
        return subs

    channels = sorted(required_channels(graph, resolver))
    return detect_new(channels, yt, state, lambda: now, fetch=fetch, backfill=backfill), subs


def run(
    graph: Graph,
    yt: YouTubeAPI,
    state: State,
    meter: QuotaMeter,
    now: datetime,
    budget: int,
    allow_any_playlist: bool = False,
    dry_run: bool = False,
    fetch: Fetcher = fetch_url,
    sleep: Callable[[float], None] = time.sleep,
    backfill: int = 0,
) -> Report:
    """Execute one run. In a dry run `state` must be a throwaway copy. Quota used is persisted even on failure."""
    day = quota_day(now)
    report = Report(dry_run=dry_run, budget=budget)
    run_id = None if dry_run else state.start_run(now)
    try:
        _run(graph, yt, state, meter, now, day, report, allow_any_playlist, fetch, sleep, backfill)
    except QuotaExceededError:
        report.quota_stopped = True
        report.errors.append("YouTube quota exceeded; stopped cleanly. It resets at midnight Pacific time.")
    except RateLimitedError:
        report.errors.append("YouTube rate limit hit while reading; try again later.")
    finally:
        report.units_run = meter.total
        if not dry_run:
            state.add_quota(day, meter.total)
        report.units_today = state.quota_used(day)
        if not dry_run:
            state.finish_run(run_id, now, _summary(report))
    return report


def _summary(r: Report) -> dict:
    return {
        "new_videos": r.new_videos, "filed": dict(r.filed), "deferred": r.deferred, "errors": len(r.errors),
        "units": r.units_run,
    }


def _run(graph, yt, state, meter, now, day, report, allow_any, fetch, sleep, backfill) -> None:
    dests = _destinations(graph)
    owned = {p["id"]: p["snippet"]["title"] for p in yt.my_playlists()}
    report.titles = {pid: owned.get(pid) or t or pid for pid, t in dests.items()}
    if not allow_any:
        foreign = sorted(set(dests) - set(owned))
        if foreign:
            report.config_errors = [
                f"destination playlist {pid} is not owned by the signed-in account; "
                "set allow_any_playlist=true to override" for pid in foreign
            ]
            return

    detection, subs = detect_recent(graph, yt, state, now, fetch, backfill)
    videos = detection.videos
    report.new_videos = len(videos)
    report.fallback_channels = len(detection.fallback_channels)
    for ch, kind in detection.errors:
        report.errors.append(f"channel {ch}: {kind}")
    if not videos:
        return

    result = execute(graph, videos, now, subscriptions=subs)
    by_id = {v.video_id: v for v in videos}
    for nid, counts in result.node_counts.items():
        if counts.get("drop"):
            report.dropped_by_node[nid] = counts["drop"]
    filings: dict[str, list[str]] = {}
    for vid, pid in result.filings:
        filings.setdefault(vid, []).append(pid)

    if report.dry_run:
        _project(report, state, day, result, by_id, filings, meter)
        return

    for v in videos:
        if v.video_id not in filings:  # nothing to file: fully handled
            state.mark_seen(v.video_id, v.channel_id, now)

    stop = False  # quota error: nothing more can be inserted this run
    for v in videos:
        pids = filings.get(v.video_id)
        if not pids:
            continue
        failed: list[str] = []
        unfinished = False
        for pid in pids:
            if state.is_filed(v.video_id, pid):
                report.already += 1
                continue
            if pid in report.full_playlists:
                failed.append(f"playlist {pid} is full")
                continue
            if stop or state.quota_used(day) + meter.total + INSERT_COST > report.budget:
                report.deferred += 1
                unfinished = True
                continue
            outcome = _insert(yt, state, report, v, pid, now, sleep)
            if outcome == "quota":
                stop = True
                report.quota_stopped = True
                report.deferred += 1
                unfinished = True
            elif outcome == "deferred":
                report.deferred += 1
                unfinished = True
            elif outcome:
                failed.append(outcome)
        _settle(state, report, v, now, failed, unfinished)


def _insert(yt, state, report, v: Video, pid: str, now, sleep) -> str | None:
    """Insert one filing. None on success/already there; 'quota'/'deferred' when it must wait; else an error text."""
    for attempt in (1, 2):
        try:
            yt.insert_into_playlist(pid, v.video_id)
        except AlreadyInPlaylistError:
            state.mark_filed(v.video_id, pid, now)
            report.already += 1
            return None
        except QuotaExceededError:
            report.errors.append("YouTube quota exceeded; stopped inserting. It resets at midnight Pacific time.")
            return "quota"
        except RateLimitedError:
            if attempt == 1:
                sleep(RATE_LIMIT_PAUSE_SECONDS)
                continue
            report.errors.append(f"rate limited inserting {v.video_id}; deferred to the next run")
            return "deferred"
        except PlaylistFullError:
            report.full_playlists.add(pid)
            report.errors.append(f"playlist {report.titles.get(pid, pid)} is full ({PLAYLIST_LIMIT} items); skipping it")
            return f"playlist {pid} is full"
        except YouTubeError as err:
            report.errors.append(f"insert {v.video_id} into {report.titles.get(pid, pid)}: {err}")
            return str(err)
        state.mark_filed(v.video_id, pid, now)
        report.filed[pid] += 1
        return None
    return "deferred"  # unreachable


def _settle(state, report, v: Video, now, failed: list[str], unfinished: bool) -> None:
    """Mark the video seen when fully handled; count a failed run and give up after the cap."""
    if failed:
        if state.record_failure(v.video_id, "; ".join(failed)[:300], MAX_FAILED_RUNS):
            state.mark_seen(v.video_id, v.channel_id, now)
            report.gave_up.append(v.video_id)
    elif not unfinished:
        state.mark_seen(v.video_id, v.channel_id, now)


def _project(report, state, day, result, by_id, filings, meter) -> None:
    report.result = result
    report.video_titles = {vid: v.title for vid, v in by_id.items()}
    for vid, pids in filings.items():
        for pid in pids:
            if state.is_filed(vid, pid):
                report.already += 1
            else:
                report.projected[pid] += 1
    inserts = sum(report.projected.values())
    room = max(0, (report.budget - state.quota_used(day) - meter.total) // INSERT_COST)
    report.deferred = max(0, inserts - room)
    report.units_projected = meter.total + min(inserts, room) * INSERT_COST


def render(report: Report, graph: Graph | None = None) -> str:
    lines: list[str] = []
    if report.config_errors:
        lines.append("Config errors (nothing was inserted):")
        lines += [f"  {e}" for e in report.config_errors]
    lines.append(f"{'DRY RUN - nothing written. ' if report.dry_run else ''}New videos: {report.new_videos}")
    if report.fallback_channels:
        lines.append(f"  ({report.fallback_channels} channel(s) read via the uploads playlist because RSS failed)")
    if report.dry_run and report.result:
        lines += _render_nodes(report)
        lines.append("Projected filings per playlist:")
        for pid, n in sorted(report.projected.items()):
            lines.append(f"  {report.titles.get(pid, pid)}: {n}")
        if not report.projected:
            lines.append("  none")
        if report.already:
            lines.append(f"  ({report.already} already filed)")
    else:
        lines.append("Filed per playlist:")
        for pid, n in sorted(report.filed.items()):
            lines.append(f"  {report.titles.get(pid, pid)}: {n}")
        if not report.filed:
            lines.append("  none")
        if report.already:
            lines.append(f"Already filed (skipped): {report.already}")
        if report.dropped_by_node:
            lines.append("Dropped per filter:")
            lines += [f"  {nid}: {n}" for nid, n in sorted(report.dropped_by_node.items())]
    if report.deferred:
        lines.append(f"Deferred to the next run: {report.deferred} filing(s)")
    if report.gave_up:
        lines.append(f"Gave up after {MAX_FAILED_RUNS} failed runs: {', '.join(report.gave_up)}")
    if report.errors:
        lines.append("Errors:")
        lines += [f"  {e}" for e in report.errors]
    if report.dry_run:
        lines.append(
            f"Projected quota: {report.units_projected} units for a real run ({report.units_run} spent on reads by "
            f"this dry run, which still costs quota: RSS is free, videos.list costs 1 unit per 50 videos); "
            f"{report.units_today} already used today / budget {report.budget}."
        )
    else:
        lines.append(f"Quota: {report.units_run} units this run, {report.units_today} used today / budget {report.budget}")
    return "\n".join(lines)


def _render_nodes(report: Report) -> list[str]:
    lines = ["Per node:"]
    r = report.result
    for nid, counts in sorted(r.node_counts.items()):
        if not counts:
            continue
        stats = ", ".join(f"{k} {n}" for k, n in sorted(counts.items()))
        titles = []
        for vid, trace in r.traces.items():
            for t in trace:
                if t.node_id == nid and len(titles) < 3:
                    titles.append(f"{t.outcome}: {report.video_titles.get(vid, vid)}")
        lines.append(f"  {nid}: {stats}")
        lines += [f"      {t}" for t in titles]
    return lines


def preview_node(graph: Graph, node_id: str, yt, state, now, fetch, backfill) -> list[Video]:
    """Recent unseen videos that pass the node (read-only: `state` must be a throwaway copy)."""
    detection, subs = detect_recent(graph, yt, state, now, fetch, backfill)
    result = execute(graph, detection.videos, now, subscriptions=subs)
    by_id = {v.video_id: v for v in detection.videos}
    return [by_id[vid] for vid, trace in result.traces.items() if any(t.node_id == node_id and t.outcome != "drop" for t in trace)]
