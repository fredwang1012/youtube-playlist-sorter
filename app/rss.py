"""Free new-upload feed: YouTube's per-channel Atom feed costs no API quota."""
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = "youtube-playlist-sorter/0.1 (+https://github.com/fredwang1012/youtube-playlist-sorter)"
TIMEOUT_SECONDS = 15
MAX_BYTES = 2_000_000
FEED_SIZE = 15  # YouTube returns at most this many entries
# the feed endpoint intermittently answers 404/5xx for valid channels; a retry is free, the API fallback is not
ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

Fetcher = Callable[[str], bytes]


class RssError(Exception):
    """The feed could not be fetched or parsed."""


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    channel_id: str
    title: str
    published: datetime


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = resp.read(MAX_BYTES + 1)
    except OSError as err:  # URLError and timeouts are OSError subclasses
        raise RssError(f"Could not fetch feed: {err}") from None
    if len(data) > MAX_BYTES:
        raise RssError("Feed response is too large.")
    return data


def parse_feed(xml: bytes) -> list[FeedEntry]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise RssError("Feed is not valid XML.") from None
    entries = []
    for e in root.findall("atom:entry", NS):
        try:
            entries.append(
                FeedEntry(
                    video_id=e.findtext("yt:videoId", namespaces=NS).strip(),
                    channel_id=e.findtext("yt:channelId", namespaces=NS).strip(),
                    title=(e.findtext("atom:title", namespaces=NS) or "").strip(),
                    published=datetime.fromisoformat(e.findtext("atom:published", namespaces=NS).strip()),
                )
            )
        except (AttributeError, ValueError):
            raise RssError("Feed entry is missing a required field.") from None
    return entries


def fetch_feed(
    channel_id: str, fetch: Fetcher = fetch_url, sleep: Callable[[float], None] = time.sleep
) -> list[FeedEntry]:
    url = FEED_URL.format(channel_id=channel_id)
    for attempt in range(ATTEMPTS):
        try:
            return parse_feed(fetch(url))
        except RssError:
            if attempt == ATTEMPTS - 1:
                raise
            sleep(RETRY_DELAY_SECONDS * (attempt + 1))
    raise AssertionError("unreachable")
