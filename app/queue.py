"""The watch queue: one owned playlist, listed from YouTube with a short cache, where a tick removes the item.

tick is the only caller of playlistItems.delete. It refuses any item whose playlist is not the saved queue playlist,
checked against state immediately before the call. Every other playlist stays insert-only.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from app.detect import parse_duration
from app.run import quota_day
from app.state import State
from app.youtube import DELETE_COST, NotFoundError, QuotaMeter, YouTubeAPI

QUEUE_KEY = "queue_playlist"  # kv key holding the saved queue playlist id

TickResult = Literal["removed", "already_gone"]


class QueueError(Exception):
    pass


class QuotaCeilingError(Exception):
    pass


@dataclass(frozen=True)
class QueueItem:
    item_id: str
    video_id: str
    title: str
    channel: str
    duration_seconds: int | None
    thumbnail: str | None
    added_at: str
    playlist_id: str


class ItemRef(Protocol):
    item_id: str
    video_id: str
    playlist_id: str


_cache: dict[str, tuple[datetime, list[QueueItem]]] = {}  # playlist id -> (fetched at, items)


def invalidate() -> None:
    _cache.clear()


def _charge(state: State, day: str, units: int) -> None:
    if units:
        state.add_quota(day, units)


def get_queue_playlist(state: State) -> str | None:
    row = state.kv_get(QUEUE_KEY)
    return row[0] if row else None


def set_queue_playlist(
    state: State, playlist_id: str, owned_titles: dict[str, str], confirm_title: str, now: datetime
) -> None:
    """Save the queue playlist; the owner must own it and have typed its exact title."""
    if playlist_id not in owned_titles:
        raise QueueError("That playlist isn't one of yours.")
    if confirm_title != owned_titles[playlist_id]:
        raise QueueError("The typed title doesn't match the playlist's title.")
    state.kv_set(QUEUE_KEY, playlist_id, now)
    invalidate()


def _thumbnail(snippet: dict[str, Any]) -> str | None:
    thumbs = snippet.get("thumbnails") or {}
    for size in ("medium", "default", "high"):
        if thumbs.get(size, {}).get("url"):
            return thumbs[size]["url"]
    return None


def _to_item(raw: dict[str, Any], playlist_id: str, durations: dict[str, int]) -> QueueItem:
    sn = raw.get("snippet", {})
    video_id = raw.get("contentDetails", {}).get("videoId") or sn.get("resourceId", {}).get("videoId", "")
    return QueueItem(
        item_id=raw["id"],
        video_id=video_id,
        title=sn.get("title", ""),
        channel=sn.get("videoOwnerChannelTitle") or "",  # absent for deleted and private videos
        duration_seconds=durations.get(video_id),
        thumbnail=_thumbnail(sn),
        added_at=sn.get("publishedAt", ""),
        playlist_id=playlist_id,
    )


def list_queue(yt: YouTubeAPI, state: State, now: datetime, max_age: timedelta = timedelta(seconds=60)) -> list[QueueItem]:
    """The queue playlist's items, newest added first; [] when no queue is set."""
    playlist_id = get_queue_playlist(state)
    if not playlist_id:
        return []
    cached = _cache.get(playlist_id)
    if cached and now - cached[0] < max_age:
        return cached[1]
    before = yt.meter.total
    try:
        raw = yt.queue_items(playlist_id)
        ids = [i for i in dict.fromkeys(r.get("contentDetails", {}).get("videoId") for r in raw) if i]
        videos = yt.videos(ids) if ids else []
    finally:
        _charge(state, quota_day(now), yt.meter.total - before)
    durations = {v["id"]: parse_duration(v.get("contentDetails", {}).get("duration", "")) for v in videos}
    items = sorted((_to_item(r, playlist_id, durations) for r in raw), key=lambda i: i.added_at, reverse=True)
    _cache[playlist_id] = (now, items)
    return items


def tick(
    yt: YouTubeAPI, state: State, meter: QuotaMeter, now: datetime, item: ItemRef, budget_ceiling: int
) -> TickResult:
    """Remove a watched item from the queue playlist. A 404 means it was already removed elsewhere: success.

    Never touches the filed or seen records, so a ticked video is never filed again.
    """
    invalidate()
    day = quota_day(now)
    used = state.quota_used(day)
    if used + DELETE_COST > budget_ceiling:
        raise QuotaCeilingError(
            f"Today's YouTube quota is nearly used up ({used} of {budget_ceiling} units); "
            "ticks work again after midnight Pacific time."
        )
    before = meter.total
    try:
        # the allow-list: delete only from the queue playlist saved right now
        saved = get_queue_playlist(state)
        if not saved or not item.playlist_id or item.playlist_id != saved:
            raise QueueError("queue changed, refresh")
        yt.remove_playlist_item(item.item_id)
        return "removed"
    except NotFoundError:
        return "already_gone"
    finally:
        _charge(state, day, meter.total - before)
        invalidate()
