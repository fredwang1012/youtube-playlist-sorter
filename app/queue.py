"""The watch queue: one owned playlist, listed from YouTube with a short cache, where a tick removes the item.

tick is the only caller of playlistItems.delete. It deletes an item only when that item id is in a listing of the saved
queue playlist (fetched from YouTube, never taken from the caller) with the expected video id. Every other playlist
stays insert-only.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from app.detect import parse_duration
from app.run import quota_day
from app.state import State
from app.youtube import DELETE_COST, NotFoundError, YouTubeAPI

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


def _find(items: list[QueueItem], item_id: str, playlist_id: str) -> QueueItem | None:
    return next((i for i in items if i.item_id == item_id and i.playlist_id == playlist_id), None)


def _ceiling_check(state: State, day: str, budget_ceiling: int) -> None:
    used = state.quota_used(day)
    if used + DELETE_COST > budget_ceiling:
        raise QuotaCeilingError(
            f"Today's YouTube quota is nearly used up ({used} of {budget_ceiling} units); "
            "ticks work again after midnight Pacific time."
        )


def tick(yt: YouTubeAPI, state: State, now: datetime, item: ItemRef, budget_ceiling: int) -> TickResult:
    """Remove a watched item from the queue playlist.

    playlistItems.delete takes only an item id, so the caller's playlist_id proves nothing. The delete is sent only
    when item_id appears in a listing of the saved queue playlist (the cache, else one fresh fetch) with the same
    video id. An id not in the queue, or a 404 from the delete (removed elsewhere since the listing), is
    "already_gone" either way. Listing reads are charged by list_queue; the delete's units are charged here.
    Never touches the filed or seen records, so a ticked video is never filed again.
    """
    day = quota_day(now)
    try:
        saved = get_queue_playlist(state)
        # a quick refusal for a UI showing an old queue; not what authorises the delete
        if not saved or item.playlist_id != saved:
            raise QueueError("queue changed, refresh")
        _ceiling_check(state, day, budget_ceiling)
        before = yt.meter.total
        found = _find(list_queue(yt, state, now), item.item_id, saved)
        if found is None and yt.meter.total == before:  # cache hit without the item: fetch once more
            found = _find(list_queue(yt, state, now, max_age=timedelta(0)), item.item_id, saved)
        if found is None:
            return "already_gone"
        if found.video_id != item.video_id:
            raise QueueError("queue changed, refresh")
        _ceiling_check(state, day, budget_ceiling)  # the listing may have spent units
        if get_queue_playlist(state) != saved:
            raise QueueError("queue changed, refresh")
        before = yt.meter.total
        try:
            yt.remove_playlist_item(found.item_id)
            return "removed"
        except NotFoundError:
            return "already_gone"
        finally:
            _charge(state, day, yt.meter.total - before)
    finally:
        invalidate()
