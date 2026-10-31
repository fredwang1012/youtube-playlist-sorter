"""Queue playlist: listing with a short cache, and tick-to-remove guarded by the saved queue id and the tick ceiling."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import queue
from app.queue import QueueError, QuotaCeilingError
from app.run import quota_day
from app.state import State
from app.youtube import DELETE_COST, NotFoundError, QuotaMeter, RealYouTube
from tests.test_auth import FakeService, http_error
from tests.test_detect import CH
from tests.test_run import DAY, NOW, PL1, FakeYT, go, graph

PLA = "PLaaaaaaaaaaA"
PLB = "PLbbbbbbbbbbB"
CEILING = 9500


def pl_item(item_id, video_id, playlist_id, added_at, title=None, channel="Some Channel", thumbs=True):
    snippet = {
        "playlistId": playlist_id,
        "title": title or f"title {video_id}",
        "videoOwnerChannelTitle": channel,
        "publishedAt": added_at,
        "resourceId": {"kind": "youtube#video", "videoId": video_id},
    }
    if thumbs:
        snippet["thumbnails"] = {"default": {"url": f"https://i.ytimg.com/vi/{video_id}/default.jpg"},
                                 "medium": {"url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"}}
    return {"id": item_id, "snippet": snippet, "contentDetails": {"videoId": video_id}}


@pytest.fixture
def state():
    s = State()
    yield s
    s.close()


def with_queue(state, playlist_id=PLB):
    queue.set_queue_playlist(state, playlist_id, {playlist_id: "Queue"}, "Queue", NOW)


def yt_with(playlist_id=PLB, n=2):
    yt = FakeYT()
    yt.queues[playlist_id] = [
        pl_item(f"item{i}", f"v{i}", playlist_id, f"2026-10-2{i}T12:00:00Z") for i in range(1, n + 1)
    ]
    return yt


def ref(item_id, video_id, playlist_id):
    return SimpleNamespace(item_id=item_id, video_id=video_id, playlist_id=playlist_id)


def tick(yt, state, item, ceiling=CEILING):
    return queue.tick(yt, state, yt.meter, NOW, item, ceiling)


# --- RealYouTube additions ---

def test_queue_items_pages_at_50_and_costs_1_per_page():
    svc = FakeService(playlistItems=[{"items": [{"id": "a"}], "nextPageToken": "t"}, {"items": [{"id": "b"}]}])
    meter = QuotaMeter()
    out = RealYouTube(svc, meter).queue_items(PLB)
    assert [i["id"] for i in out] == ["a", "b"]
    assert meter.total == 2
    kw = svc.log[0][2]
    assert (kw["part"], kw["playlistId"], kw["maxResults"]) == ("snippet,contentDetails", PLB, 50)
    assert svc.log[1][2]["pageToken"] == "t"


def test_remove_playlist_item_costs_50_and_maps_404():
    svc = FakeService(playlistItems=["", http_error(404, "playlistItemNotFound")])
    meter = QuotaMeter()
    yt = RealYouTube(svc, meter)
    yt.remove_playlist_item("item1")
    assert svc.log[0][1:] == ("delete", {"id": "item1"})
    assert meter.by_call == {"playlistItems.delete": DELETE_COST}
    with pytest.raises(NotFoundError):
        yt.remove_playlist_item("item1")
    assert meter.total == 2 * DELETE_COST


# --- queue setting ---

def test_set_queue_requires_owned_and_typed_title(state):
    owned = {PLA: "Later", PLB: "Queue"}
    with pytest.raises(QueueError):
        queue.set_queue_playlist(state, PLB, owned, "queue", NOW)  # wrong title
    with pytest.raises(QueueError):
        queue.set_queue_playlist(state, "PLnotmine", owned, "Queue", NOW)  # not owned
    with pytest.raises(QueueError):
        queue.set_queue_playlist(state, PLB, owned, "Later", NOW)  # another playlist's title
    assert queue.get_queue_playlist(state) is None
    queue.set_queue_playlist(state, PLB, owned, "Queue", NOW)
    assert queue.get_queue_playlist(state) == PLB


# --- listing ---

def test_list_queue_empty_when_unset(state):
    yt = yt_with()
    assert queue.list_queue(yt, state, NOW) == []
    assert yt.calls == [] and yt.meter.total == 0


def test_list_queue_maps_and_sorts(state):
    with_queue(state)
    yt = yt_with()
    items = queue.list_queue(yt, state, NOW)
    assert [i.item_id for i in items] == ["item2", "item1"]  # newest added first
    first = items[0]
    assert first == queue.QueueItem(
        item_id="item2", video_id="v2", title="title v2", channel="Some Channel", duration_seconds=600,
        thumbnail="https://i.ytimg.com/vi/v2/mqdefault.jpg", added_at="2026-10-22T12:00:00Z", playlist_id=PLB,
    )
    assert yt.calls == ["queue_items", "videos"]
    assert state.quota_used(DAY) == 2


def test_list_queue_handles_unavailable_videos(state):
    with_queue(state)
    yt = FakeYT()
    yt.queues[PLB] = [pl_item("item1", "gone1", PLB, "2026-10-21T12:00:00Z", title="Deleted video",
                              channel=None, thumbs=False)]
    yt.videos = lambda ids: (yt._read("videos"), [])[1]  # deleted/private videos are missing from videos.list
    [item] = queue.list_queue(yt, state, NOW)
    assert (item.title, item.channel, item.duration_seconds, item.thumbnail) == ("Deleted video", "", None, None)


def test_list_queue_cached_for_60s(state):
    with_queue(state)
    yt = yt_with()
    queue.list_queue(yt, state, NOW)
    queue.list_queue(yt, state, NOW + timedelta(seconds=30))
    assert yt.calls == ["queue_items", "videos"]
    queue.list_queue(yt, state, NOW + timedelta(seconds=61))
    assert yt.calls == ["queue_items", "videos"] * 2


def test_list_queue_cache_follows_queue_change(state):
    with_queue(state, PLB)
    yt = yt_with(PLB)
    yt.queues[PLA] = [pl_item("itemA", "vA", PLA, "2026-10-25T12:00:00Z")]
    queue.list_queue(yt, state, NOW)
    queue.set_queue_playlist(state, PLA, {PLA: "Other"}, "Other", NOW)
    assert [i.item_id for i in queue.list_queue(yt, state, NOW)] == ["itemA"]


# --- tick ---

def test_tick_removes_and_charges_50(state):
    with_queue(state)
    yt = yt_with()
    [newest, _] = queue.list_queue(yt, state, NOW)
    before = state.quota_used(DAY)
    assert tick(yt, state, newest) == "removed"
    assert yt.deletes == ["item2"]
    assert state.quota_used(DAY) == before + DELETE_COST
    yt.calls.clear()
    assert [i.item_id for i in queue.list_queue(yt, state, NOW)] == ["item1"]  # cache invalidated: refetched
    assert yt.calls == ["queue_items", "videos"]


def test_tick_never_touches_filed_or_seen(state):
    with_queue(state)
    state.mark_seen("v2", CH, NOW)
    state.mark_filed("v2", PLB, NOW)
    yt = yt_with()
    tick(yt, state, ref("item2", "v2", PLB))
    assert state.seen(["v2"]) == {"v2"} and state.is_filed("v2", PLB)  # never re-added by a later run


def test_tick_already_gone_is_success(state):
    """removed in the YouTube app since the listing; the delete 404s."""
    with_queue(state)
    yt = yt_with()
    queue.list_queue(yt, state, NOW)
    yt.queues[PLB] = [i for i in yt.queues[PLB] if i["id"] != "item2"]  # removed elsewhere
    assert tick(yt, state, ref("item2", "v2", PLB)) == "already_gone"
    assert yt.calls.count("delete") == 1 and yt.deletes == []
    assert state.quota_used(quota_day(NOW)) == 2 + DELETE_COST  # the listing plus the failed delete, as charged
    yt.calls.clear()
    assert [i.item_id for i in queue.list_queue(yt, state, NOW)] == ["item1"]
    assert yt.calls == ["queue_items", "videos"]


def test_tick_refused_when_queue_changed(state):
    """the UI still shows the old queue."""
    with_queue(state, PLB)
    yt = yt_with(PLB)
    yt.queues[PLA] = [pl_item("itemA", "vA", PLA, "2026-10-25T12:00:00Z")]
    queue.list_queue(yt, state, NOW)
    with pytest.raises(QueueError, match="queue changed, refresh"):
        tick(yt, state, ref("itemA", "vA", PLA))
    assert "delete" not in yt.calls and yt.deletes == []
    yt.calls.clear()
    queue.list_queue(yt, state, NOW)
    assert yt.calls == ["queue_items", "videos"]  # cache invalidated on refusal too

    state.kv_delete(queue.QUEUE_KEY)  # queue unset
    for item in (ref("item1", "v1", PLB), ref("item1", "v1", None), ref("item1", "v1", "")):
        with pytest.raises(QueueError, match="queue changed, refresh"):
            tick(yt, state, item)
    assert "delete" not in yt.calls and yt.deletes == []


def test_tick_respects_ceiling(state):
    with_queue(state)
    yt = yt_with()
    state.add_quota(DAY, 9460)
    with pytest.raises(QuotaCeilingError):
        tick(yt, state, ref("item1", "v1", PLB))
    assert yt.calls == [] and state.quota_used(DAY) == 9460

    state.add_quota(DAY, -10)  # 9450 used: 9450 + 50 <= 9500
    assert tick(yt, state, ref("item1", "v1", PLB)) == "removed"
    assert state.quota_used(DAY) == 9500


def test_inserts_still_stop_at_budget(state):
    state.mark_channel_known(CH, NOW)
    state.add_quota(DAY, 7990)  # ticks would still have headroom; inserts do not
    yt = FakeYT()
    r = go(graph(), yt, state, ["v1"], budget=8000)
    assert yt.inserts == [] and r.deferred == 1


def test_delete_never_called_for_other_playlists(state):
    ids = [PLA, PLB, PL1, "PLcccccccccc", "", None]
    for saved in [PLA, PLB, None]:
        if saved:
            with_queue(state, saved)
        else:
            state.kv_delete(queue.QUEUE_KEY)
        for target in ids:
            yt = FakeYT()
            yt.queues = {p: [pl_item("item", "v", p, "2026-10-25T12:00:00Z")] for p in ids if p}
            try:
                result = tick(yt, state, ref("item", "v", target))
            except QueueError:
                result = None
            if saved is not None and target == saved:
                assert result == "removed" and yt.deletes == ["item"]
            else:
                assert result is None and "delete" not in yt.calls, (saved, target)
