"""Run command: detection -> graph -> inserts with the quota guard, against a fake YouTube client."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import auth, cli, run as runner
from app.config import settings
from app.graph import parse_graph
from app.state import State
from app.youtube import (
    INSERT_COST,
    AlreadyInPlaylistError,
    PlaylistFullError,
    QuotaExceededError,
    QuotaMeter,
    RealYouTube,
    ServerError,
    RateLimitedError,
    YouTubeError,
    _map_error,
)
from tests.test_auth import FakeService, http_error
from tests.test_detect import CH, feed_xml, video_item

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)  # 02:00 Pacific, same day
DAY = "2026-10-30"
PL1 = "PLaaaaaaaaaa1"
PL2 = "PLaaaaaaaaaa2"
OTHER = "PLzzzzzzzzzz9"


class FakeYT:
    def __init__(self, owned=(PL1, PL2), subs=()):
        self.meter = QuotaMeter()
        self.owned = list(owned)
        self.subs = list(subs)
        self.inserts = []
        self.insert_errors = {}  # (playlist, video) -> exceptions to raise in turn (insert not applied)
        self.accept_then_raise = {}  # same, but YouTube applies the insert before the error reaches the caller
        self.present = set()
        self.calls = []

    def _read(self, name):
        self.calls.append(name)
        self.meter.record(name, 1)

    def my_playlists(self):
        self._read("playlists")
        return [{"id": p, "snippet": {"title": f"Title {p}"}} for p in self.owned]

    def subscriptions(self):
        self._read("subscriptions")
        return [{"snippet": {"resourceId": {"channelId": c}}} for c in self.subs]

    def videos(self, ids):
        self._read("videos")
        return [video_item(v) for v in ids]

    def insert_into_playlist(self, playlist_id, video_id):
        self.calls.append("insert")
        self.meter.record("insert", INSERT_COST)
        errs = self.insert_errors.get((playlist_id, video_id))
        if errs:
            raise errs.pop(0)
        self.inserts.append((playlist_id, video_id))
        self.present.add((playlist_id, video_id))
        errs = self.accept_then_raise.get((playlist_id, video_id))
        if errs:
            raise errs.pop(0)

    def playlist_contains(self, playlist_id, video_id):
        self._read("contains")
        return (playlist_id, video_id) in self.present


def graph(*playlists):
    playlists = playlists or (PL1,)
    nodes = [{"id": "s", "type": "source.channel", "params": {"channel_id": CH}}]
    edges = []
    for i, pl in enumerate(playlists):
        nodes.append({"id": f"d{i}", "type": "destination.playlist", "params": {"playlist_id": pl}})
        edges.append({"from": "s", "to": f"d{i}"})
    return parse_graph({"version": 1, "nodes": nodes, "edges": edges})


def keyword_graph(term):
    return parse_graph({"version": 1, "nodes": [
        {"id": "s", "type": "source.channel", "params": {"channel_id": CH}},
        {"id": "kw", "type": "filter.keyword", "params": {"include": [term], "fields": ["title"]}},
        {"id": "d", "type": "destination.playlist", "params": {"playlist_id": PL1}}],
        "edges": [{"from": "s", "to": "kw"}, {"from": "kw", "to": "d"}]})


def fetcher(ids):
    return lambda url: feed_xml(CH, ids)


@pytest.fixture
def state():
    s = State()
    s.mark_channel_known(CH, NOW)  # not a first run: every feed entry is new
    yield s
    s.close()


def go(g, yt, state, ids, budget=8000, **kw):
    kw.setdefault("sleep", lambda _s: None)
    return runner.run(g, yt, state, yt.meter, NOW, budget, fetch=fetcher(ids), **kw)


def fresh_meter(yt):
    yt.meter = QuotaMeter()


def test_files_new_videos_and_records_state(state):
    yt = FakeYT()
    r = go(graph(), yt, state, ["v1", "v2"])
    assert yt.inserts == [(PL1, "v1"), (PL1, "v2")]
    assert r.new_videos == 2 and r.filed[PL1] == 2
    assert state.is_filed("v1", PL1) and state.seen(["v1", "v2"]) == {"v1", "v2"}
    assert state.quota_used(DAY) == yt.meter.total == 2 + 2 * INSERT_COST  # playlists + videos + inserts
    out = runner.render(r)
    assert "Title PLaaaaaaaaaa1: 2" in out and f"{yt.meter.total} used today / budget 8000" in out


def test_no_duplicate_inserts_across_runs(state):
    yt = FakeYT()
    go(graph(), yt, state, ["v1", "v2"])
    fresh_meter(yt)
    r = go(graph(), yt, state, ["v1", "v2"])
    assert len(yt.inserts) == 2 and r.new_videos == 0
    # a recorded filing is never inserted again even if the video shows up unseen
    state.mark_filed("v3", PL1, NOW)
    go(graph(), yt, state, ["v1", "v2", "v3"])
    assert len(yt.inserts) == 2


def test_already_filed_but_unseen_is_skipped(state):
    yt = FakeYT()
    state.mark_filed("v1", PL1, NOW)
    r = go(graph(), yt, state, ["v1"])
    assert yt.inserts == [] and r.already == 1 and state.seen(["v1"]) == {"v1"}


def test_already_in_playlist_on_youtube_marks_filed(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [AlreadyInPlaylistError("dup")]
    r = go(graph(), yt, state, ["v1"])
    assert state.is_filed("v1", PL1) and state.seen(["v1"]) == {"v1"} and r.errors == []


def test_dry_run_writes_nothing(state):
    yt = FakeYT()
    snap = State.throwaway_copy()
    r = go(graph(), yt, snap, ["v1", "v2"], dry_run=True)
    snap.close()
    assert yt.inserts == [] and "insert" not in yt.calls
    assert state.quota_used(DAY) == 0 and state.seen(["v1", "v2"]) == set() and not state.is_filed("v1", PL1)
    assert state._db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert r.projected[PL1] == 2
    out = runner.render(r)
    assert "DRY RUN" in out and "Title PLaaaaaaaaaa1: 2" in out and "still costs quota" in out
    assert f"{2 + 2 * INSERT_COST} units for a real run" in out
    assert "s: pass 2" in out and "pass: title v1" in out


def test_dry_run_with_no_state_file_creates_nothing():
    State.throwaway_copy().close()
    assert not (settings.data_dir / "state.db").exists()


def test_dry_run_reports_drops_and_at_most_three_examples():
    snap = State.throwaway_copy()
    snap.mark_channel_known(CH, NOW)
    r = go(keyword_graph("v1"), FakeYT(), snap, ["v1", "v2", "v3", "v4", "v5"], dry_run=True)
    out = runner.render(r).splitlines()
    assert "  kw: drop 4, pass 1" in out
    block = out[out.index("  kw: drop 4, pass 1") + 1:]
    examples = []
    for line in block:
        if not line.startswith("      "):
            break
        examples.append(line)
    assert examples == ["      pass: title v1", "      drop: title v2", "      drop: title v3"]


def test_budget_stop_and_carry_over(state):
    yt = FakeYT()
    r = go(graph(), yt, state, ["v1", "v2", "v3"], budget=2 + 2 * INSERT_COST)  # 2 reads + two inserts
    assert yt.inserts == [(PL1, "v1"), (PL1, "v2")] and r.deferred == 1
    assert state.pending_rows() == [("v3", PL1, CH, 0)]  # carried over in the pending queue
    assert "Deferred to the next run: 1" in runner.render(r)
    fresh_meter(yt)
    r = go(graph(), yt, state, ["v1", "v2", "v3"])
    assert yt.inserts == [(PL1, "v1"), (PL1, "v2"), (PL1, "v3")] and r.deferred == 0
    assert state.pending_rows() == [] and state.is_filed("v3", PL1)


def test_deferred_video_gone_from_feed_is_still_filed(state):
    yt = FakeYT()
    go(graph(), yt, state, ["v1", "v2", "v3"], budget=2 + 2 * INSERT_COST)
    fresh_meter(yt)
    r = go(graph(), yt, state, ["v9"])  # v3 has dropped out of the feed
    assert (PL1, "v3") in yt.inserts and r.filed[PL1] == 2 and r.deferred == 0


def test_budget_counts_units_already_used_today(state):
    state.add_quota(DAY, 7990)
    yt = FakeYT()
    r = go(graph(), yt, state, ["v1"], budget=8000)
    assert yt.inserts == [] and r.deferred == 1 and state.pending_rows() == [("v1", PL1, CH, 0)]


def test_insert_error_does_not_stop_others_and_gives_up_after_three_runs(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [YouTubeError("boom")] * 3
    r = go(graph(), yt, state, ["v1", "v2"])
    assert yt.inserts == [(PL1, "v2")] and len(r.errors) == 1 and "boom" in r.errors[0]
    assert state.seen(["v1", "v2"]) == {"v2"} and state.failures("v1") == 1
    fresh_meter(yt)
    go(graph(), yt, state, ["v1", "v2"])
    assert state.failures("v1") == 2 and state.seen(["v1"]) == set()
    fresh_meter(yt)
    r = go(graph(), yt, state, ["v1", "v2"])
    assert r.gave_up == ["v1"] and state.seen(["v1"]) == {"v1"}
    assert state.gave_up() == [("v1", "boom")]
    assert "Gave up" in runner.render(r)


def test_failed_filing_does_not_block_other_playlist_and_only_it_is_retried(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [YouTubeError("boom")]
    go(graph(PL1, PL2), yt, state, ["v1"])
    assert yt.inserts == [(PL2, "v1")] and not state.is_filed("v1", PL1)
    fresh_meter(yt)
    go(graph(PL1, PL2), yt, state, ["v1"])
    assert yt.inserts == [(PL2, "v1"), (PL1, "v1")]


def test_quota_exceeded_stops_cleanly_and_saves_state(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v2")] = [QuotaExceededError("q")]
    r = go(graph(), yt, state, ["v1", "v2", "v3"])
    assert yt.inserts == [(PL1, "v1")] and yt.calls.count("insert") == 2
    assert r.quota_stopped and r.deferred == 2
    assert state.is_filed("v1", PL1) and not state.is_filed("v2", PL1)
    assert [row[0] for row in state.pending_rows()] == ["v2", "v3"]  # carried over
    assert all(row[3] == 0 for row in state.pending_rows())  # a rejected insert is not a maybe-sent one
    assert state.quota_used(DAY) == yt.meter.total  # the failed call is metered and persisted
    assert state._db.execute("SELECT finished_at FROM runs").fetchone()[0] is not None


def test_quota_exceeded_during_detection_saves_state(state):
    class Boom(FakeYT):
        def videos(self, ids):
            self._read("videos")
            raise QuotaExceededError("q")

    yt = Boom()
    r = go(graph(), yt, state, ["v1"])
    assert r.quota_stopped and state.quota_used(DAY) == yt.meter.total == 2


def test_rate_limit_retries_once_then_succeeds(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [RateLimitedError("r")]
    sleeps = []
    r = go(graph(), yt, state, ["v1"], sleep=sleeps.append)
    assert sleeps == [2] and yt.inserts == [(PL1, "v1")] and r.deferred == 0 and r.errors == []


def test_rate_limit_twice_defers_video(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [RateLimitedError("r")] * 2
    r = go(graph(), yt, state, ["v1", "v2"])
    assert r.deferred == 1 and yt.inserts == [(PL1, "v2")]
    assert state.pending_rows() == [("v1", PL1, CH, 0)] and state.failures("v1") == 0


def test_playlist_full_skips_further_inserts(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [PlaylistFullError("full")]
    r = go(graph(), yt, state, ["v1", "v2"])
    assert yt.calls.count("insert") == 1 and PL1 in r.full_playlists
    assert any("full" in e for e in r.errors)
    assert state.failures("v1") == 0 and state.failures("v2") == 0  # a full playlist is not a failure
    assert state.seen(["v1", "v2"]) == set()


def test_foreign_playlist_is_a_config_error_before_any_work(state):
    yt = FakeYT()
    r = go(graph(PL1, OTHER), yt, state, ["v1"])
    assert r.config_errors and OTHER in r.config_errors[0]
    assert yt.inserts == [] and "videos" not in yt.calls and state.seen(["v1"]) == set()
    assert "Config errors" in runner.render(r)


def test_allow_any_playlist_permits_foreign(state):
    yt = FakeYT()
    r = go(graph(OTHER), yt, state, ["v1"], allow_any_playlist=True)
    assert yt.inserts == [(OTHER, "v1")] and not r.config_errors


def test_unmatched_video_is_marked_seen_and_counted(state):
    yt = FakeYT()
    r = go(keyword_graph("nomatch"), yt, state, ["v1"])
    assert state.seen(["v1"]) == {"v1"} and r.dropped_by_node == {"kw": 1} and yt.inserts == []
    assert "kw: 1" in runner.render(r)


def sub_graph():
    return parse_graph({"version": 1, "nodes": [
        {"id": "s", "type": "source.subscriptions"},
        {"id": "d", "type": "destination.playlist", "params": {"playlist_id": PL1}}],
        "edges": [{"from": "s", "to": "d"}]})


def test_subscriptions_resolved_and_cached_24h(state):
    yt = FakeYT(subs=[CH])
    go(sub_graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")] and yt.calls.count("subscriptions") == 1
    fresh_meter(yt)
    go(sub_graph(), yt, state, ["v2"])
    assert yt.calls.count("subscriptions") == 1  # cached
    later = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
    runner.run(sub_graph(), yt, state, yt.meter, later, 8000, fetch=fetcher(["v3"]))
    assert yt.calls.count("subscriptions") == 2


def test_quota_day_is_pacific():
    assert runner.quota_day(datetime(2026, 10, 30, 6, 59, tzinfo=timezone.utc)) == "2026-10-29"
    assert runner.quota_day(datetime(2026, 10, 30, 7, 1, tzinfo=timezone.utc)) == "2026-10-30"


def test_error_mapping_for_duplicates_and_full_playlists():
    assert isinstance(_map_error(http_error(409, "videoAlreadyInPlaylist"), "x"), AlreadyInPlaylistError)
    assert isinstance(_map_error(http_error(403, "playlistContainsMaximumNumberOfVideos"), "x"), PlaylistFullError)


def test_preview_node_is_read_only(state):
    snap = State.throwaway_copy()
    snap.mark_channel_known(CH, NOW)
    passing = runner.preview_node(keyword_graph("v1"), "kw", FakeYT(), snap, NOW, fetcher(["v1", "v2"]), 0)
    snap.close()
    assert [v.video_id for v in passing] == ["v1"]
    assert state.seen(["v1", "v2"]) == set()


# --- CLI ---

def test_cli_auth_expired_exit_2(monkeypatch, capsys):
    def boom():
        raise auth.AuthExpiredError("Google rejected the stored token")

    monkeypatch.setattr(cli, "_client", boom)
    assert cli.main(["run", "--graph", "graphs/example.yaml"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "expired or was revoked" in err and "7 days" in err


def test_cli_dry_run_and_preview_end_to_end(monkeypatch, capsys, tmp_path):
    gpath = tmp_path / "g.json"
    gpath.write_text(json.dumps({"version": 1, "nodes": [
        {"id": "s", "type": "source.channel", "params": {"channel_id": CH}},
        {"id": "d", "type": "destination.playlist", "params": {"playlist_id": PL1}}],
        "edges": [{"from": "s", "to": "d"}]}))
    yt = FakeYT()
    monkeypatch.setattr(cli, "_client", lambda: (yt, yt.meter))
    monkeypatch.setattr(runner, "fetch_url", fetcher(["v1"]))
    assert cli.main(["run", "--graph", str(gpath), "--dry-run", "--backfill", "5"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert yt.inserts == [] and not (settings.data_dir / "state.db").exists()
    assert cli.main(["preview", "--graph", str(gpath), "--node", "s"]) == 0
    assert "v1" in capsys.readouterr().out
    assert cli.main(["preview", "--graph", str(gpath), "--node", "nope"]) == 1
    capsys.readouterr()


# --- crash recovery, pending queue, accounting ---

class Crash(BaseException):
    """Simulates the process dying; not caught by the run's error handling."""


def test_crash_after_accepted_insert_does_not_reinsert(state):
    yt = FakeYT()
    yt.accept_then_raise[(PL1, "v1")] = [Crash()]
    with pytest.raises(Crash):
        go(graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")] and state.pending_rows() == [("v1", PL1, CH, 1)]
    assert not state.is_filed("v1", PL1) and state.seen(["v1"]) == set()
    fresh_meter(yt)
    r = go(graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")]  # still exactly one copy
    assert state.is_filed("v1", PL1) and state.pending_rows() == [] and state.seen(["v1"]) == {"v1"}
    assert yt.meter.by_call["contains"] == 1 and r.errors == []


def test_timeout_after_accepted_insert_is_verified_not_reinserted(state):
    yt = FakeYT()
    yt.accept_then_raise[(PL1, "v1")] = [TimeoutError("slow")]
    r = go(graph(), yt, state, ["v1"])
    assert any("outcome unknown" in e for e in r.errors) and state.pending_rows() == [("v1", PL1, CH, 1)]
    assert r.deferred == 1
    fresh_meter(yt)
    go(graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")] and state.is_filed("v1", PL1)
    assert yt.meter.total == 2  # playlists.list + the 1-unit check


def test_timeout_before_insert_is_checked_then_inserted_once(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [TimeoutError("lost")]
    go(graph(), yt, state, ["v1"])
    assert yt.inserts == []
    fresh_meter(yt)
    go(graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")] and yt.calls.count("contains") == 1
    assert state.is_filed("v1", PL1) and state.pending_rows() == []


def test_server_error_is_treated_as_ambiguous(state):
    yt = FakeYT()
    yt.accept_then_raise[(PL1, "v1")] = [ServerError("503")]
    go(graph(), yt, state, ["v1"])
    assert state.pending_rows() == [("v1", PL1, CH, 1)] and state.failures("v1") == 0
    fresh_meter(yt)
    go(graph(), yt, state, ["v1"])
    assert yt.inserts == [(PL1, "v1")]


def test_rejected_insert_leaves_no_maybe_sent_row(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [YouTubeError("bad")]
    go(graph(), yt, state, ["v1"])
    assert state.pending_rows() == []


def test_deferred_filings_never_count_toward_give_up(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [YouTubeError("boom")] * 3
    yt.insert_errors[(PL2, "v1")] = [RateLimitedError("r")] * 6
    for _ in range(3):
        fresh_meter(yt)
        go(graph(PL1, PL2), yt, state, ["v1"])
    assert state.failures("v1") == 3 and state.gave_up() == [] and state.seen(["v1"]) == set()
    assert state.pending_rows() == [("v1", PL2, CH, 0)]
    fresh_meter(yt)
    go(graph(PL1, PL2), yt, state, ["v1"])  # no errors left: both filings go through
    assert state.is_filed("v1", PL1) and state.is_filed("v1", PL2) and state.seen(["v1"]) == {"v1"}


def test_budget_rechecked_before_rate_limit_retry(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [RateLimitedError("r")]
    # 2 reads + one insert fits; after the failed (still charged) insert a second one would not
    r = go(graph(), yt, state, ["v1"], budget=2 + INSERT_COST + 10)
    assert yt.calls.count("insert") == 1 and r.deferred == 1 and yt.inserts == []


def test_units_are_saved_as_they_are_spent(state, monkeypatch):
    yt = FakeYT()

    def die(*a, **k):
        raise Crash()

    monkeypatch.setattr(state, "complete_filing", die)
    with pytest.raises(Crash):
        go(graph(), yt, state, ["v1", "v2"])
    assert state.quota_used(DAY) == yt.meter.total == 2 + INSERT_COST


def test_dry_run_leaves_real_state_file_byte_identical(state):
    path = settings.data_dir / "state.db"
    state.add_quota(DAY, 123)
    state.mark_filed("old", PL1, NOW)
    state.queue_pending("q1", PL1, CH, NOW, "deferred")
    state.mark_seen("seen1", CH, NOW)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    files = sorted(p.name for p in path.parent.iterdir())
    snap = State.throwaway_copy(path)
    r = go(graph(), FakeYT(), snap, ["v1", "v2"], dry_run=True)
    snap.close()
    assert r.projected[PL1] == 3  # two new videos plus the carried-over one
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in path.parent.iterdir()) == files


def test_playlist_contains_costs_one_unit_and_is_read_only():
    svc = FakeService(playlistItems=[{"items": [{"id": "x"}]}, {"items": []}])
    meter = QuotaMeter()
    yt = RealYouTube(svc, meter)
    assert yt.playlist_contains("PL1", "vid") is True and yt.playlist_contains("PL1", "other") is False
    assert meter.total == 2 and meter.by_call == {"playlistItems.list": 2}
    assert svc.log[0] == ("playlistItems", "list", {"part": "id", "playlistId": "PL1", "videoId": "vid", "maxResults": 1})


def test_5xx_maps_to_server_error():
    assert isinstance(_map_error(http_error(503, "backendError"), "x"), ServerError)


# --- follow-ups: full playlists, pending failures, orphaned rows ---

def test_playlist_full_keeps_the_filing_queued(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [PlaylistFullError("full")]
    go(graph(), yt, state, ["v1", "v2"])
    assert {row[0] for row in state.pending_rows()} == {"v1", "v2"}  # the one that hit it and the one skipped
    assert all(row[3] == 0 for row in state.pending_rows())


def test_full_playlist_is_remembered_for_24_hours(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [PlaylistFullError("full")]
    go(graph(), yt, state, ["v1"])
    assert state.kv_get("playlist_full:" + PL1) is not None
    fresh_meter(yt)
    before = yt.calls.count("insert")
    later = NOW + timedelta(hours=23)
    r = runner.run(graph(), yt, state, yt.meter, later, 8000, fetch=fetcher(["v1"]), sleep=lambda _s: None)
    assert yt.calls.count("insert") == before and PL1 in r.full_playlists
    assert [row[0] for row in state.pending_rows()] == ["v1"]


def test_full_playlist_is_tried_again_after_24_hours(state):
    yt = FakeYT()
    yt.insert_errors[(PL1, "v1")] = [PlaylistFullError("full")]
    go(graph(), yt, state, ["v1"])
    fresh_meter(yt)
    later = NOW + timedelta(hours=25)
    r = runner.run(graph(), yt, state, yt.meter, later, 8000, fetch=fetcher(["v1"]), sleep=lambda _s: None)
    assert yt.inserts == [(PL1, "v1")] and not r.full_playlists
    assert state.kv_get("playlist_full:" + PL1) is None and state.pending_rows() == []


def test_carried_over_filing_that_keeps_failing_is_given_up_after_three_runs(state):
    yt = FakeYT()
    state.queue_pending("old", PL1, CH, NOW, "deferred")  # not in the feed any more
    yt.insert_errors[(PL1, "old")] = [YouTubeError("nope")] * 3
    for n in (1, 2):
        fresh_meter(yt)
        go(graph(), yt, state, [])
        assert state.failures("old") == n and state.pending_rows() == [("old", PL1, CH, 0)]
    fresh_meter(yt)
    r = go(graph(), yt, state, [])
    assert r.gave_up == ["old"] and state.pending_rows() == [] and state.gave_up() == [("old", "nope")]
    assert yt.inserts == []


def test_pending_rows_for_other_playlists_are_reported_and_kept(state):
    yt = FakeYT()
    state.queue_pending("old", OTHER, CH, NOW, "deferred")
    r = go(graph(), yt, state, [])
    assert r.orphaned == 1 and state.pending_rows() == [("old", OTHER, CH, 0)]
    assert "1 queued filings target playlists not in this graph" in runner.render(r)
    assert yt.inserts == []
