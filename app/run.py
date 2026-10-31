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
    READ_COST,
    AlreadyInPlaylistError,
    PlaylistFullError,
    QuotaExceededError,
    QuotaMeter,
    RateLimitedError,
    ServerError,
    YouTubeAPI,
    YouTubeError,
)

QUOTA_TZ = ZoneInfo("America/Los_Angeles")  # the daily quota resets at midnight Pacific
SUBS_CACHE_KEY = "subscriptions"
SUBS_CACHE_TTL = timedelta(hours=24)
MAX_FAILED_RUNS = 3
RATE_LIMIT_PAUSE_SECONDS = 2
PLAYLIST_LIMIT = 5000
FULL_KEY = "playlist_full:"  # kv key prefix; the timestamp says when the playlist was found full
FULL_TTL = timedelta(hours=24)


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
    orphaned: int = 0  # queued filings whose playlist is not a destination in this graph (kept, not deleted)
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


class _Defer(Exception):
    """An API call was not made (budget) or was refused (rate limit, quota); the filing waits for the next run."""


class _Ctx:
    """Per-run working state. Quota units are saved to state as they are spent, so a hard kill loses none."""

    def __init__(self, yt, state: State, meter: QuotaMeter, now: datetime, day: str, report: Report, sleep) -> None:
        self.yt, self.state, self.meter, self.now, self.day = yt, state, meter, now, day
        self.report, self.sleep = report, sleep
        self.flushed = 0
        self.stop = False  # quota error: nothing more can be sent this run

    def used(self) -> int:
        return self.state.quota_used(self.day) + self.meter.total - self.flushed

    def flush(self) -> None:
        delta = self.meter.total - self.flushed
        if delta:
            self.state.add_quota(self.day, delta)
            self.flushed = self.meter.total

    def call(self, fn: Callable[[], object], cost: int):
        """Run an API call under the budget guard with one rate-limit retry (budget re-checked before the retry)."""
        for attempt in (1, 2):
            if self.stop or self.used() + cost > self.report.budget:
                raise _Defer()
            try:
                return fn()
            except RateLimitedError:
                if attempt == 2:
                    self.report.errors.append("rate limited; deferred to the next run")
                    raise _Defer() from None
                self.sleep(RATE_LIMIT_PAUSE_SECONDS)
            except QuotaExceededError:
                self.stop = self.report.quota_stopped = True
                self.report.errors.append("YouTube quota exceeded; stopped. It resets at midnight Pacific time.")
                raise _Defer() from None
            finally:
                if not self.report.dry_run:
                    self.flush()

    def file(self, video_id: str, channel_id: str, pid: str) -> tuple[str, str]:
        """Put one video into one playlist. Returns (status, message): done, queued (still pending), failed or full."""
        st, rep, now = self.state, self.report, self.now
        if st.is_filed(video_id, pid):
            st.drop_pending(video_id, pid)
            rep.already += 1
            return "done", ""
        if pid in rep.full_playlists:
            st.queue_pending(video_id, pid, channel_id, now, "playlist full")
            return "full", ""
        was_pending = st.pending_attempts(video_id, pid) is not None
        label = f"{video_id} -> {rep.titles.get(pid, pid)}"
        try:
            if st.pending_attempts(video_id, pid):  # an earlier attempt may have been accepted: look before inserting
                try:
                    present = self.call(lambda: self.yt.playlist_contains(pid, video_id), READ_COST)
                except _Defer:
                    raise
                except Exception as err:
                    rep.errors.append(f"could not verify {label} ({type(err).__name__}); will retry next run")
                    return "queued", ""
                if present:
                    st.complete_filing(video_id, pid, now)
                    rep.already += 1
                    return "done", ""
                st.clear_attempts(video_id, pid)

            def attempt() -> None:
                st.begin_attempt(video_id, pid, channel_id, now)  # written before the insert is sent
                try:
                    self.yt.insert_into_playlist(pid, video_id)
                except ServerError:
                    raise  # may have been applied: leave the attempt on record
                except YouTubeError:
                    st.undo_attempt(video_id, pid)  # the API rejected it
                    raise

            self.call(attempt, INSERT_COST)
        except _Defer:
            st.queue_pending(video_id, pid, channel_id, now, "deferred")
            return "queued", ""
        except AlreadyInPlaylistError:
            st.complete_filing(video_id, pid, now)
            rep.already += 1
            return "done", ""
        except PlaylistFullError:
            st.queue_pending(video_id, pid, channel_id, now, "playlist full")
            st.kv_set(FULL_KEY + pid, "1", now)
            rep.full_playlists.add(pid)
            rep.errors.append(f"playlist {rep.titles.get(pid, pid)} is full ({PLAYLIST_LIMIT} items); skipping it")
            return "full", ""
        except ServerError as err:
            rep.errors.append(f"insert {label}: {err} Outcome unknown; will verify next run.")
            return "queued", ""
        except YouTubeError as err:
            if not was_pending:  # a carried-over filing stays queued; the caller counts the failure
                st.drop_pending(video_id, pid)
            rep.errors.append(f"insert {label}: {err}")
            return "failed", str(err)
        except Exception as err:  # timeout, network error: the insert may or may not have happened
            rep.errors.append(f"insert {label}: {type(err).__name__}; outcome unknown, will verify next run")
            return "queued", ""
        st.complete_filing(video_id, pid, now)
        rep.filed[pid] += 1
        return "done", ""


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
    ctx = _Ctx(yt, state, meter, now, day, report, sleep)
    run_id = None if dry_run else state.start_run(now)
    try:
        _run(ctx, graph, allow_any_playlist, fetch, backfill)
    except QuotaExceededError:
        report.quota_stopped = True
        report.errors.append("YouTube quota exceeded; stopped cleanly. It resets at midnight Pacific time.")
    except RateLimitedError:
        report.errors.append("YouTube rate limit hit while reading; try again later.")
    finally:
        report.units_run = meter.total
        if not dry_run:
            ctx.flush()
            dests = set(_destinations(graph))
            report.deferred = sum(1 for _v, pid, _c, _a in state.pending_rows() if pid in dests)
        report.orphaned = sum(1 for _v, pid, _c, _a in state.pending_rows() if pid not in _destinations(graph))
        report.units_today = state.quota_used(day)
        if not dry_run:
            state.finish_run(run_id, now, _summary(report))
    return report


def _summary(r: Report) -> dict:
    return {
        "new_videos": r.new_videos, "filed": dict(r.filed), "deferred": r.deferred, "errors": len(r.errors),
        "units": r.units_run,
    }


def _run(ctx: _Ctx, graph: Graph, allow_any: bool, fetch: Fetcher, backfill: int) -> None:
    state, report, yt, now = ctx.state, ctx.report, ctx.yt, ctx.now
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

    for pid in dests:  # playlists found full within the last 24 hours are skipped without an API call
        entry = state.kv_get(FULL_KEY + pid)
        if entry and now - entry[1] < FULL_TTL:
            report.full_playlists.add(pid)
            report.errors.append(f"playlist {report.titles[pid]} was full within the last 24 hours; skipping it")
        elif entry:
            state.kv_delete(FULL_KEY + pid)

    # Filings carried over from earlier runs go first; they do not depend on the RSS window.
    handled: dict[tuple[str, str], tuple[str, str]] = {}
    if not report.dry_run:
        for vid, pid, ch, _attempts in state.pending_rows():
            if pid in dests:
                handled[(vid, pid)] = ctx.file(vid, ch, pid)
                if ctx.stop:
                    return

    detection, subs = detect_recent(graph, yt, state, now, fetch, backfill)
    videos = detection.videos
    report.new_videos = len(videos)
    report.fallback_channels = len(detection.fallback_channels)
    for ch, kind in detection.errors:
        report.errors.append(f"channel {ch}: {kind}")

    result = execute(graph, videos, now, subscriptions=subs)
    by_id = {v.video_id: v for v in videos}
    for nid, counts in result.node_counts.items():
        if counts.get("drop"):
            report.dropped_by_node[nid] = counts["drop"]
    filings: dict[str, list[str]] = {}
    for vid, pid in result.filings:
        filings.setdefault(vid, []).append(pid)

    if report.dry_run:
        _project(report, state, ctx.day, result, by_id, filings, ctx.meter, set(dests))
        return

    settled: set[tuple[str, str]] = set()
    for v in videos:
        pids = filings.get(v.video_id)
        if not pids:  # nothing to file: fully handled
            state.mark_seen(v.video_id, v.channel_id, now)
            continue
        results = [handled.get((v.video_id, pid)) or ctx.file(v.video_id, v.channel_id, pid) for pid in pids]
        settled.update((v.video_id, pid) for pid in pids)
        _settle(state, report, v, now, pids, results)
    _settle_pending(state, report, now, handled, settled)


def _settle(state: State, report: Report, v: Video, now, pids: list[str], results: list[tuple[str, str]]) -> None:
    """Mark the video seen once every filing is done or queued (the queue carries it over).

    A definite failure counts toward the give-up cap, but a video with filings still unfinished (queued or in a full
    playlist) is never given up, and those unfinished filings are never counted as failures.
    """
    statuses = {s for s, _ in results}
    failed = [m for s, m in results if s == "failed"]
    unfinished = bool(statuses & {"queued", "full"})
    if failed:
        if state.record_failure(v.video_id, "; ".join(failed)[:300], MAX_FAILED_RUNS, allow_give_up=not unfinished):
            state.mark_seen(v.video_id, v.channel_id, now)
            report.gave_up.append(v.video_id)
            for pid, (s, _m) in zip(pids, results):
                if s == "failed":
                    state.drop_pending(v.video_id, pid)
    elif "full" not in statuses:
        state.mark_seen(v.video_id, v.channel_id, now)


def _settle_pending(state: State, report: Report, now, handled, settled) -> None:
    """Count definite failures of carried-over filings that no feed video settled, with the same 3-run give-up rule."""
    by_video: dict[str, list[tuple[str, tuple[str, str]]]] = {}
    for (vid, pid), res in handled.items():
        if (vid, pid) not in settled:
            by_video.setdefault(vid, []).append((pid, res))
    for vid, items in by_video.items():
        failed = [(pid, m) for pid, (s, m) in items if s == "failed"]
        if not failed:
            continue
        unfinished = any(s in ("queued", "full") for _p, (s, _m) in items)
        if state.record_failure(vid, "; ".join(m for _p, m in failed)[:300], MAX_FAILED_RUNS, allow_give_up=not unfinished):
            report.gave_up.append(vid)
            for pid, _m in failed:
                state.drop_pending(vid, pid)


def _project(report, state, day, result, by_id, filings, meter, dests) -> None:
    report.result = result
    report.video_titles = {vid: v.title for vid, v in by_id.items()}
    wanted = {(vid, pid) for vid, pids in filings.items() for pid in pids}
    wanted |= {(vid, pid) for vid, pid, _c, _a in state.pending_rows() if pid in dests}  # carried over
    for vid, pid in sorted(wanted):
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
    if report.orphaned:
        lines.append(f"{report.orphaned} queued filings target playlists not in this graph")
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
