"""YouTube Data API wrapper. Every call records its quota cost; the only write is playlistItems.insert."""
import json
from typing import Any, Callable, Protocol

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

READ_COST = 1
INSERT_COST = 50
VIDEOS_BATCH = 50


class YouTubeError(Exception):
    """Base class for API failures; messages never include response bodies."""


class QuotaExceededError(YouTubeError):
    pass


class ForbiddenError(YouTubeError):
    pass


class NotFoundError(YouTubeError):
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
    def insert_into_playlist(self, playlist_id: str, video_id: str) -> None: ...


def _reason(err: HttpError) -> str:
    try:
        return json.loads(err.content)["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def _map_error(err: HttpError, what: str) -> YouTubeError:
    status = getattr(err.resp, "status", None)
    reason = _reason(err)
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return QuotaExceededError(f"YouTube quota exceeded during {what}. It resets at midnight Pacific time.")
    if status == 404 or reason in ("notFound", "playlistNotFound", "channelNotFound", "videoNotFound"):
        return NotFoundError(f"Not found during {what}.")
    if status == 403:
        return ForbiddenError(f"Forbidden during {what}; check the token scope and playlist ownership.")
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
                self._svc.videos().list(part="snippet,contentDetails", id=",".join(batch), maxResults=VIDEOS_BATCH),
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

    def insert_into_playlist(self, playlist_id: str, video_id: str) -> None:
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        }
        self._run(self._svc.playlistItems().insert(part="snippet", body=body), "playlistItems.insert", INSERT_COST)
