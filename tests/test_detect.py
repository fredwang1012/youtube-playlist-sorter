import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import detect, rss
from app.config import settings
from app.detect import detect_new, parse_duration
from app.state import State
from app.youtube import QuotaMeter, RealYouTube
from tests.test_auth import FakeService

FIXTURE = Path(__file__).parent / "fixtures" / "channel_feed.xml"
CH = "UCaaaaaaaaaaaaaaaaaaaaaa"
UPLOADS = "UU" + CH[2:]
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def feed_xml(ch, ids):
    entries = "".join(
        f"<entry><yt:videoId>{v}</yt:videoId><yt:channelId>{ch}</yt:channelId><title>t {v}</title>"
        f"<published>2026-10-{10 + n:02d}T00:00:00+00:00</published></entry>"
        for n, v in enumerate(ids)
    )
    ns = 'xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom"'
    return f"<feed {ns}>{entries}</feed>".encode()


def video_item(vid, ch=CH, duration="PT10M", live="none"):
    return {
        "id": vid,
        "snippet": {"channelId": ch, "title": f"title {vid}", "description": "desc", "tags": ["a", "b"],
                    "categoryId": "27", "liveBroadcastContent": live, "publishedAt": "2026-10-29T12:00:00Z"},
        "contentDetails": {"duration": duration},
    }


class FakeYT:
    """Records calls; videos.list answers every id except those starting with 'gone'."""

    def __init__(self, uploads=None):
        self.uploads = uploads or {}
        self.calls = []

    def channels_uploads_playlist(self, channel_id):
        self.calls.append(("uploads_playlist", channel_id))
        return "UU" + channel_id[2:]

    def playlist_items(self, playlist_id, max_items):
        self.calls.append(("playlist_items", playlist_id, max_items))
        return [
            {"snippet": {"title": f"t {v}", "publishedAt": f"2026-10-{10 + n % 15:02d}T00:00:00+00:00"},
             "contentDetails": {"videoId": v}}
            for n, v in enumerate(self.uploads[playlist_id][:max_items])
        ]

    def videos(self, ids):
        self.calls.append(("videos", list(ids)))
        return [video_item(v) for v in ids if not v.startswith("gone")]


@pytest.fixture
def state():
    s = State()
    yield s
    s.close()


def feed_fetcher(feeds):
    """feeds: channel_id -> feed bytes, or an exception to raise."""
    def fetch(url):
        result = feeds[url.split("channel_id=")[1]]
        if isinstance(result, Exception):
            raise result
        return result
    return fetch


def run(state, yt, feeds, **kw):
    return detect_new(list(feeds), yt, state, lambda: NOW, feed_fetcher(feeds), **kw)


# --- rss ---

def test_parse_fixture_feed():
    entries = rss.parse_feed(FIXTURE.read_bytes())
    assert [e.video_id for e in entries] == ["vid00000001", "vid00000002", "vid00000003"]
    assert entries[0].title == "Newest & best"
    assert entries[0].channel_id == CH
    assert entries[0].published == datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [b"<feed", b"<feed xmlns='http://www.w3.org/2005/Atom'><entry/></feed>"])
def test_bad_feed_raises_rss_error(bad):
    with pytest.raises(rss.RssError):
        rss.parse_feed(bad)


def test_fetch_feed_builds_url_without_network():
    urls = []
    rss.fetch_feed(CH, lambda url: urls.append(url) or FIXTURE.read_bytes())
    assert urls == [f"https://www.youtube.com/feeds/videos.xml?channel_id={CH}"]


def test_fetch_url_sets_user_agent_and_timeout(monkeypatch):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout):
        seen["ua"], seen["timeout"] = req.get_header("User-agent"), timeout
        return Resp()

    monkeypatch.setattr(rss.urllib.request, "urlopen", fake_urlopen)
    assert rss.fetch_url("https://example.invalid/feed") == b"ok"
    assert seen == {"ua": rss.USER_AGENT, "timeout": 15}
    assert "@" not in rss.USER_AGENT.replace("youtube-playlist-sorter", "")


def test_fetch_url_network_error_becomes_rss_error(monkeypatch):
    def boom(req, timeout):
        raise rss.urllib.error.URLError("offline")

    monkeypatch.setattr(rss.urllib.request, "urlopen", boom)
    with pytest.raises(rss.RssError):
        rss.fetch_url("https://example.invalid/feed")


# --- duration / shorts ---

@pytest.mark.parametrize("iso,seconds", [
    ("PT45S", 45), ("PT3M", 180), ("PT1H2M3S", 3723), ("PT1H", 3600), ("P1DT1H", 90000),
    ("P0D", 0), ("", 0), ("junk", 0),
])
def test_parse_duration(iso, seconds):
    assert parse_duration(iso) == seconds


@pytest.mark.parametrize("duration,live,short", [
    ("PT59S", "none", True), ("PT3M", "none", True), ("PT3M1S", "none", False),
    ("PT30S", "live", False), ("P0D", "upcoming", False),
])
def test_shorts_heuristic(duration, live, short):
    assert detect.to_video(video_item("v", duration=duration, live=live)).is_short is short


def test_to_video_maps_metadata():
    v = detect.to_video(video_item("abc", duration="PT10M"))
    assert (v.video_id, v.channel_id, v.title, v.description) == ("abc", CH, "title abc", "desc")
    assert v.tags == ("a", "b") and v.category_id == "27" and v.duration_seconds == 600
    assert v.published_at == datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


# --- state ---

def test_state_filed_is_unique_per_video_and_playlist(state):
    assert state.mark_filed("v1", "PL1", NOW) is True
    assert state.mark_filed("v1", "PL1", NOW) is False
    assert state.mark_filed("v1", "PL2", NOW) is True
    assert state.is_filed("v1", "PL1") and not state.is_filed("v2", "PL1")


def test_state_quota_accumulates_per_day(state):
    state.add_quota("2026-10-30", 5)
    state.add_quota("2026-10-30", 50)
    state.add_quota("2026-10-31", 1)
    used = (state.quota_used("2026-10-30"), state.quota_used("2026-10-31"), state.quota_used("2026-11-01"))
    assert used == (55, 1, 0)


def test_state_runs_and_persistence(tmp_path):
    path = tmp_path / "state.db"
    s = State(path)
    run_id = s.start_run(NOW)
    s.finish_run(run_id, NOW, {"filed": 2})
    s.mark_seen("v1", CH, NOW)
    s.close()
    db = sqlite3.connect(path)
    row = db.execute("SELECT finished_at, summary FROM runs WHERE id = ?", (run_id,)).fetchone()
    db.close()
    assert row == (NOW.isoformat(), '{"filed": 2}')
    again = State(path)
    assert again.seen(["v1", "v2"]) == {"v1"}
    again.close()


def test_state_default_path_is_under_data_dir():
    State().close()
    assert (settings.data_dir / "state.db").is_file()


# --- detection ---

def test_first_run_marks_existing_items_seen_without_flood(state):
    yt = FakeYT()
    out = run(state, yt, {CH: FIXTURE.read_bytes()})
    assert out.videos == []
    assert state.seen(["vid00000001", "vid00000002", "vid00000003"]) == {"vid00000001", "vid00000002", "vid00000003"}
    assert yt.calls == []  # no enrichment, no quota spent


def test_first_run_backfill_returns_newest_n(state):
    out = run(state, FakeYT(), {CH: FIXTURE.read_bytes()}, backfill=2)
    assert [v.video_id for v in out.videos] == ["vid00000001", "vid00000002"]
    assert state.seen(["vid00000003"]) == {"vid00000003"}


def test_second_run_returns_only_new_and_dedupes(state):
    run(state, FakeYT(), {CH: FIXTURE.read_bytes()})
    feed = feed_xml(CH, ["new1", "vid00000001", "vid00000002"])
    out = run(state, FakeYT(), {CH: feed})
    assert [v.video_id for v in out.videos] == ["new1"]
    assert out.videos[0].duration_seconds == 600
    state.mark_seen("new1", CH, NOW)
    assert run(state, FakeYT(), {CH: feed}).videos == []


def test_enrichment_is_one_request_across_channels(state):
    chans = [f"UC{c}" * 3 for c in "bcd"]
    for c in chans:
        state.mark_channel_known(c, NOW)
    feeds = {c: feed_xml(c, [f"{c}-{i}" for i in range(5)]) for c in chans}
    yt = FakeYT()
    out = run(state, yt, feeds)
    assert len(out.videos) == 15
    assert [len(c[1]) for c in yt.calls if c[0] == "videos"] == [15]


def test_enrichment_batches_at_50_through_real_wrapper(state):
    chans = [f"UC{c}" * 3 for c in "bcdef"]
    for c in chans:
        state.mark_channel_known(c, NOW)
    feeds = {c: feed_xml(c, [f"{c}-{i}" for i in range(14)]) for c in chans}  # 70 new ids
    ids = [f"{c}-{i}" for c in chans for i in range(14)]
    svc = FakeService(videos=[{"items": [video_item(v) for v in ids[:50]]},
                              {"items": [video_item(v) for v in ids[50:]]}])
    meter = QuotaMeter()
    out = detect_new(chans, RealYouTube(svc, meter), state, lambda: NOW, feed_fetcher(feeds))
    assert len(out.videos) == 70
    assert [len(c[2]["id"].split(",")) for c in svc.log] == [50, 20]
    assert meter.by_call == {"videos.list": 2}


def test_deleted_videos_are_dropped(state):
    state.mark_channel_known(CH, NOW)
    out = run(state, FakeYT(), {CH: feed_xml(CH, ["gone1", "ok1"])})
    assert [v.video_id for v in out.videos] == ["ok1"]


def test_rss_failure_falls_back_to_uploads_playlist(state):
    state.mark_channel_known(CH, NOW)
    state.mark_seen("old", CH, NOW)
    yt = FakeYT(uploads={UPLOADS: ["new1", "old"]})
    out = run(state, yt, {CH: rss.RssError("down")})
    assert [v.video_id for v in out.videos] == ["new1"]
    assert out.fallback_channels == [CH]
    assert ("playlist_items", UPLOADS, 50) in yt.calls


def test_full_unknown_feed_triggers_fallback_capped_at_50(state):
    state.mark_channel_known(CH, NOW)
    state.mark_seen("old", CH, NOW)
    feed_ids = [f"f{i}" for i in range(15)]
    yt = FakeYT(uploads={UPLOADS: feed_ids + [f"u{i}" for i in range(60)] + ["old"]})
    out = run(state, yt, {CH: feed_xml(CH, feed_ids)})
    assert out.fallback_channels == [CH]
    assert ("playlist_items", UPLOADS, 50) in yt.calls
    assert len(out.videos) == 50


def test_full_feed_with_a_known_item_does_not_fall_back(state):
    state.mark_channel_known(CH, NOW)
    state.mark_seen("f14", CH, NOW)
    yt = FakeYT()
    out = run(state, yt, {CH: feed_xml(CH, [f"f{i}" for i in range(15)])})
    assert out.fallback_channels == [] and len(out.videos) == 14
    assert not any(c[0] == "playlist_items" for c in yt.calls)


def test_full_feed_on_first_run_does_not_fall_back(state):
    yt = FakeYT()
    out = run(state, yt, {CH: feed_xml(CH, [f"f{i}" for i in range(15)])})
    assert out.fallback_channels == [] and out.videos == []
    assert yt.calls == []


def test_empty_feed_still_marks_channel_known(state):
    run(state, FakeYT(), {CH: feed_xml(CH, [])})
    assert state.channel_known(CH)
    out = run(state, FakeYT(), {CH: feed_xml(CH, ["first-upload"])})
    assert [v.video_id for v in out.videos] == ["first-upload"]
