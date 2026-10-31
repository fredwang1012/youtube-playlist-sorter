"""Free new-upload feed: YouTube's per-channel Atom feed costs no API quota."""
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = "youtube-playlist-sorter/0.1 (+https://github.com/fredwang1012/youtube-playlist-sorter)"
TIMEOUT_SECONDS = 15
FEED_SIZE = 15  # YouTube returns at most this many entries

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
            return resp.read()
    except OSError as err:  # URLError and timeouts are OSError subclasses
        raise RssError(f"Could not fetch feed: {err}") from None


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


def fetch_feed(channel_id: str, fetch: Fetcher = fetch_url) -> list[FeedEntry]:
    return parse_feed(fetch(FEED_URL.format(channel_id=channel_id)))
