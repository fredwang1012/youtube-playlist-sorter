"""YouTube Data API wrapper. Every call records its quota cost.

The writes are playlistItems.insert and playlistItems.delete; delete is called only from app.queue.tick, which
checks the item belongs to the saved queue playlist first.
"""
import json
from typing import Any, Callable, Protocol

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

READ_COST = 1
INSERT_COST = 50
DELETE_COST = 50
VIDEOS_BATCH = 50


class YouTubeError(Exception):
    """Base class for API failures; messages never include response bodies."""


class QuotaExceededError(YouTubeError):
    pass


class RateLimitedError(YouTubeError):
    """Too many requests in a short time; unlike quota, retrying later the same day can work."""


class ForbiddenError(YouTubeError):
    pass


class NotFoundError(YouTubeError):
    pass


class ServerError(YouTubeError):
    """5xx: an insert that fails this way may still have been applied."""


class AlreadyInPlaylistError(YouTubeError):
    pass


class PlaylistFullError(YouTubeError):
    pass


class QuotaMeter:
    """Counts quota units spent, in total and per kind of call."""

    def __init__(self) -> None:
        self.total = 0
        self.by_call: dict[str, int] = {}

    def record(self, call: str, units: int) -> None:
        self.total += units
        self.by_call[call] = self.by_call.get(call, 0) + units


class YouTubeAPI(Protocol):
    def my_channel(self) -> dict[str, Any]: ...
    def channels_uploads_playlist(self, channel_id: str) -> str: ...
    def playlist_items(self, playlist_id: str, max_items: int) -> list[dict[str, Any]]: ...
    def videos(self, ids: list[str]) -> list[dict[str, Any]]: ...
    def my_playlists(self) -> list[dict[str, Any]]: ...
    def subscriptions(self) -> list[dict[str, Any]]: ...
    def playlist_contains(self, playlist_id: str, video_id: str) -> bool: ...
    def insert_into_playlist(self, playlist_id: str, video_id: str) -> None: ...
    def queue_items(self, playlist_id: str) -> list[dict[str, Any]]: ...
    def remove_playlist_item(self, item_id: str) -> None: ...


def _reason(err: HttpError) -> str:
    try:
        return json.loads(err.content)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def _message(err: HttpError) -> str:
    try:
        return str(json.loads(err.content)["error"].get("message", ""))
    except (ValueError, KeyError, TypeError, AttributeError):
        return ""


def _map_error(err: HttpError, what: str) -> YouTubeError:
    status = getattr(err.resp, "status", None)
    reason = _reason(err)
    if reason in ("quotaExceeded", "dailyLimitExceeded"):
        return QuotaExceededError(f"YouTube quota exceeded during {what}. It resets at midnight Pacific time.")
    if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED"):
        return RateLimitedError(f"YouTube rate limit hit during {what}; wait a bit and retry.")
    if status == 409 and (reason == "SERVICE_UNAVAILABLE" or "aborted" in _message(err).lower()):
        return ServerError(f"YouTube aborted the request during {what}.")  # may have been applied
    if reason == "videoAlreadyInPlaylist":
        return AlreadyInPlaylistError(f"Video is already in the playlist ({what}).")
    if reason == "playlistContainsMaximumNumberOfVideos":
        return PlaylistFullError(f"Playlist is full ({what}).")
    if status == 404 or reason in ("notFound", "playlistNotFound", "channelNotFound", "videoNotFound"):
        return NotFoundError(f"Not found during {what}.")
    if status == 403:
        return ForbiddenError(f"Forbidden during {what}; check the token scope and playlist ownership.")
    if status is not None and status >= 500:
        return ServerError(f"YouTube server error {status} during {what}.")
    return YouTubeError(f"YouTube API error {status} during {what}.")


class RealYouTube:
    def __init__(self, service: Any, meter: QuotaMeter) -> None:
        self._svc = service
        self.meter = meter

    @classmethod
    def from_credentials(cls, creds: Any, meter: QuotaMeter) -> "RealYouTube":
        return cls(build("youtube", "v3", credentials=creds, cache_discovery=False), meter)

    def _run(self, request: Any, call: str, units: int) -> dict[str, Any]:
        try:
            resp = request.execute()
        except HttpError as err:
            self.meter.record(call, units)  # YouTube generally charges for failed requests too
            raise _map_error(err, call) from None
        self.meter.record(call, units)
        return resp

    def _paged(self, call: str, make_request: Callable[[str | None], Any], limit: int | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = None
        while True:
            resp = self._run(make_request(token), call, READ_COST)
            items.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token or (limit is not None and len(items) >= limit):
                return items if limit is None else items[:limit]

    def my_channel(self) -> dict[str, Any]:
        resp = self._run(self._svc.channels().list(part="snippet", mine=True), "channels.list", READ_COST)
        items = resp.get("items", [])
        if not items:
            raise NotFoundError("No YouTube channel is attached to this Google account.")
        return items[0]

    def channels_uploads_playlist(self, channel_id: str) -> str:
        resp = self._run(
            self._svc.channels().list(part="contentDetails", id=channel_id), "channels.list", READ_COST
        )
        items = resp.get("items", [])
        if not items:
            raise NotFoundError(f"Channel {channel_id} not found.")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def playlist_items(self, playlist_id: str, max_items: int) -> list[dict[str, Any]]:
        if max_items <= 0:
            return []
        return self._paged(
            "playlistItems.list",
            lambda token: self._svc.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=min(50, max_items),
                pageToken=token,
            ),
            limit=max_items,
        )

    def videos(self, ids: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), VIDEOS_BATCH):
            batch = ids[i : i + VIDEOS_BATCH]
            resp = self._run(
                self._svc.videos().list(part="snippet,contentDetails", id=",".join(batch)),
                "videos.list",
                READ_COST,
            )
            out.extend(resp.get("items", []))
        return out

    def my_playlists(self) -> list[dict[str, Any]]:
        return self._paged(
            "playlists.list",
            lambda token: self._svc.playlists().list(
                part="snippet,contentDetails", mine=True, maxResults=50, pageToken=token
            ),
        )

    def subscriptions(self) -> list[dict[str, Any]]:
        return self._paged(
            "subscriptions.list",
            lambda token: self._svc.subscriptions().list(
                part="snippet", mine=True, maxResults=50, pageToken=token
            ),
        )

    def playlist_contains(self, playlist_id: str, video_id: str) -> bool:
        """Read-only (1 unit): is the video already in the playlist?"""
        resp = self._run(
            self._svc.playlistItems().list(part="id", playlistId=playlist_id, videoId=video_id, maxResults=1),
            "playlistItems.list",
            READ_COST,
        )
        return bool(resp.get("items"))

    def insert_into_playlist(self, playlist_id: str, video_id: str) -> None:
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        self._run(self._svc.playlistItems().insert(part="snippet", body=body), "playlistItems.insert", INSERT_COST)

    def queue_items(self, playlist_id: str) -> list[dict[str, Any]]:
        """Every item of the queue playlist, as raw playlistItems resources (1 unit per page of 50)."""
        return self._paged(
            "playlistItems.list",
            lambda token: self._svc.playlistItems().list(
                part="snippet,contentDetails", playlistId=playlist_id, maxResults=50, pageToken=token
            ),
        )

    def remove_playlist_item(self, item_id: str) -> None:
        """Delete one playlist item (50 units). Only app.queue.tick may call this."""
        self._run(self._svc.playlistItems().delete(id=item_id), "playlistItems.delete", DELETE_COST)
