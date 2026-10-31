"""Find uploads not yet processed: RSS first (free), the uploads playlist as a fallback, then enrich via videos.list."""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.rss import FEED_SIZE, FeedEntry, Fetcher, RssError, fetch_feed, fetch_url
from app.state import State
from app.youtube import ForbiddenError, NotFoundError, YouTubeAPI

SHORT_MAX_SECONDS = 180
FALLBACK_MAX_ITEMS = 50

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(iso: str) -> int:
    """ISO 8601 duration as returned by videos.list (e.g. PT1H2M3S) in seconds; unparseable gives 0."""
    m = _DURATION.match(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


@dataclass(frozen=True)
class Video:
    video_id: str
    channel_id: str
    title: str
    description: str
    tags: tuple[str, ...]
    category_id: str
    live_broadcast_content: str
    published_at: datetime
    duration_seconds: int
    # Heuristic: the API has no Shorts flag, so a short non-live video is assumed to be a Short.
    is_short: bool


@dataclass
class Detection:
    videos: list[Video] = field(default_factory=list)
    fallback_channels: list[str] = field(default_factory=list)  # channels read via the uploads playlist
    errors: list[tuple[str, str]] = field(default_factory=list)  # (channel_id, error class) for skipped channels
    dropped: list[str] = field(default_factory=list)  # ids missing from videos.list; already marked seen


def to_video(item: dict[str, Any]) -> Video:
    sn = item["snippet"]
    seconds = parse_duration(item.get("contentDetails", {}).get("duration", ""))
    live = sn.get("liveBroadcastContent", "none")
    return Video(
        video_id=item["id"],
        channel_id=sn["channelId"],
        title=sn.get("title", ""),
        description=sn.get("description", ""),
        tags=tuple(sn.get("tags", [])),
        category_id=sn.get("categoryId", ""),
        live_broadcast_content=live,
        published_at=datetime.fromisoformat(sn["publishedAt"]),
        duration_seconds=seconds,
        is_short=0 < seconds <= SHORT_MAX_SECONDS and live == "none",
    )


def _playlist_entries(yt: YouTubeAPI, channel_id: str, max_items: int) -> list[FeedEntry]:
    playlist = yt.channels_uploads_playlist(channel_id)
    entries = []
    for it in yt.playlist_items(playlist, max_items):
        cd, sn = it.get("contentDetails", {}), it["snippet"]
        published = cd.get("videoPublishedAt") or sn["publishedAt"]
        entries.append(FeedEntry(cd["videoId"], channel_id, sn.get("title", ""), datetime.fromisoformat(published)))
    return entries


def detect_new(
    channel_ids: list[str],
    yt: YouTubeAPI,
    state: State,
    now: Callable[[], datetime],
    fetch: Fetcher = fetch_url,
    backfill: int = 0,
    fallback_max_items: int = FALLBACK_MAX_ITEMS,
) -> Detection:
    """Unseen, enriched uploads for the channels.

    A channel's first run marks its current items as seen (no flood) and returns only the newest `backfill`.
    Returned videos are not marked seen: the caller marks them once they are handled. Videos that videos.list no
    longer returns (private/deleted) are marked seen here and listed in `dropped`. A channel that is not found or
    forbidden is recorded in `errors`; quota and rate-limit errors propagate.
    """
    result = Detection()
    pending: list[FeedEntry] = []
    for ch in channel_ids:
        try:
            fresh, via_playlist = _channel_fresh(ch, yt, state, now, fetch, backfill, fallback_max_items)
        except (NotFoundError, ForbiddenError) as err:
            # one dead or terminated channel must not stop the others; quota and rate errors still propagate
            result.errors.append((ch, type(err).__name__))
            continue
        if via_playlist:
            result.fallback_channels.append(ch)
        pending.extend(fresh)

    if pending:
        unique = list(dict.fromkeys(e.video_id for e in pending))  # the same video can appear in two feeds
        by_id = {v["id"]: v for v in yt.videos(unique)}  # batched at 50 by the wrapper
        channel_of = {e.video_id: e.channel_id for e in pending}
        for vid in unique:
            if vid not in by_id:
                # private or deleted since upload: mark seen here so it is not re-requested every run
                result.dropped.append(vid)
                state.mark_seen(vid, channel_of[vid], now())
        result.videos = [to_video(by_id[vid]) for vid in unique if vid in by_id]
    return result


def _channel_fresh(
    ch: str, yt: YouTubeAPI, state: State, now: Callable[[], datetime], fetch: Fetcher, backfill: int, fallback_max: int
) -> tuple[list[FeedEntry], bool]:
    """Unseen entries for one channel and whether the uploads playlist was needed."""
    via_playlist = False
    try:
        entries = fetch_feed(ch, fetch)
    except RssError:
        entries, via_playlist = _playlist_entries(yt, ch, fallback_max), True
    first_run = not state.channel_known(ch)
    # a full feed where nothing is known suggests more than FEED_SIZE uploads since the last run
    if not first_run and not via_playlist and len(entries) >= FEED_SIZE and not state.seen([e.video_id for e in entries]):
        entries, via_playlist = _playlist_entries(yt, ch, fallback_max), True

    seen = state.seen([e.video_id for e in entries])
    fresh = [e for e in entries if e.video_id not in seen]
    if first_run:
        fresh.sort(key=lambda e: e.published, reverse=True)
        for e in fresh[backfill:]:
            state.mark_seen(e.video_id, ch, now())
        fresh = fresh[:backfill]
        state.mark_channel_known(ch, now())
    return fresh, via_playlist
