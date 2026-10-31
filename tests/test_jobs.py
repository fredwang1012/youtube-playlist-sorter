"""Shared run entry point: the timer (CLI) and "Check now" both go through jobs.run_now under the run lock."""
import json
import threading
from datetime import timedelta

import httplib2
import pytest

from app import auth, cli, jobs, run as runner
from app.config import settings
from app.lock import run_lock
from app.state import State
from tests.test_detect import CH
from tests.test_run import NOW, PL1, FakeYT, fetcher


@pytest.fixture(autouse=True)
def _fresh_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_thread", None)
    monkeypatch.setattr(jobs, "_last", None)


def hold_lock_in_thread():
    """Hold the run lock from another thread (like a timer run); returns the release event and the thread."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with run_lock(settings.data_dir / "run.lock"):
            held.set()
            release.wait(5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    return release, t


@pytest.fixture
def gpath(tmp_path, monkeypatch):
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"version": 1, "nodes": [
        {"id": "s", "type": "source.channel", "params": {"channel_id": CH}},
        {"id": "d", "type": "destination.playlist", "params": {"playlist_id": PL1}}],
        "edges": [{"from": "s", "to": "d"}]}))
    monkeypatch.setattr(settings, "graph_path", path)
    monkeypatch.setattr(runner, "fetch_url", fetcher(["v1"]))
    s = State()
    s.mark_channel_known(CH, NOW)  # not a first run: the feed entry is new and gets filed
    s.close()
    return path


@pytest.fixture
def yt(monkeypatch):
    fake = FakeYT()
    monkeypatch.setattr(cli, "_client", lambda: (fake, fake.meter))
    return fake


def runs():
    s = State()
    try:
        return s.recent_runs()
    finally:
        s.close()


def test_run_now_records_trigger(gpath, yt):
    out = jobs.run_now("manual", make_client=lambda: (yt, yt.meter), now=NOW)
    assert out.status == "done" and out.report is not None
    summary = runs()[0]["summary"]
    assert summary["trigger"] == "manual"
    assert summary["filed"] == {PL1: 1} and summary["errors"] == []
    assert summary["units_run"] == out.report.units_run and summary["units_today"] == out.report.units_today


def test_recent_runs_newest_first_with_limit(gpath, yt):
    for trigger in ("timer", "manual", "timer"):
        jobs.run_now(trigger, make_client=lambda: (yt, yt.meter), now=NOW)
    rows = runs()
    assert [r["summary"]["trigger"] for r in rows] == ["timer", "manual", "timer"]
    assert rows[0]["id"] > rows[1]["id"] and rows[0]["finished_at"] and rows[0]["started_at"]
    s = State()
    try:
        assert len(s.recent_runs(limit=2)) == 2
    finally:
        s.close()


def test_run_now_already_running(gpath, yt):
    calls = []
    with run_lock(settings.data_dir / "run.lock"):
        out = jobs.run_now("manual", make_client=lambda: calls.append(1) or (yt, yt.meter), now=NOW)
    assert out.status == "already_running" and out.report is None
    assert calls == [] and yt.inserts == [] and runs() == []


def test_start_background_twice_runs_once(gpath, yt, monkeypatch):
    release, entered = threading.Event(), threading.Event()
    real_run = runner.run

    def blocking_run(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(runner, "run", blocking_run)
    assert jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW) == "started"
    assert entered.wait(5)
    assert jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW) == "already_running"
    release.set()
    jobs._thread.join(5)
    assert not jobs._thread.is_alive()
    assert len(runs()) == 1 and yt.inserts == [(PL1, "v1")]


def test_start_background_lost_race_to_timer_is_recorded(gpath, yt):
    with run_lock(settings.data_dir / "run.lock"):
        assert jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW) == "started"
        jobs._thread.join(5)
    out = jobs.last_outcome()
    assert out.status == "already_running" and out.trigger == "manual" and out.finished_at is not None
    assert runs() == [] and yt.inserts == []


def test_start_background_records_outcome(gpath, yt):
    assert jobs.last_outcome() is None
    jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW)
    jobs._thread.join(5)
    out = jobs.last_outcome()
    assert out.status == "done" and out.trigger == "manual" and out.report.filed == {PL1: 1}


def test_start_background_auth_expired_is_recorded(gpath):
    def boom():
        raise auth.AuthExpiredError("Google rejected the stored token")

    assert jobs.start_background("manual", make_client=boom, now=NOW) == "started"
    jobs._thread.join(5)
    out = jobs.last_outcome()
    assert out.status == "error" and "auth" in out.message.lower() and out.trigger == "manual"
    assert out.finished_at is not None


def test_is_running(gpath, yt, monkeypatch):
    s = State()
    try:
        assert not jobs.is_running(s, NOW)
        # a timer run in progress in another process: unfinished row
        rid = s.start_run(NOW)
        assert jobs.is_running(s, NOW + timedelta(minutes=30))
        assert not jobs.is_running(s, NOW + timedelta(hours=2, minutes=1))  # left by a crash
        s.finish_run(rid, NOW, {})
        assert not jobs.is_running(s, NOW)

        release, entered = threading.Event(), threading.Event()
        real_run = runner.run

        def blocking_run(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(runner, "run", blocking_run)
        jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW)
        assert entered.wait(5)
        assert jobs.is_running(s, NOW)  # our thread, before its runs row exists
        release.set()
        jobs._thread.join(5)
        assert not jobs.is_running(s, NOW)
    finally:
        s.close()


def test_recent_runs_returns_old_shaped_rows_unchanged():
    s = State()
    try:
        rid = s.start_run(NOW)
        s.finish_run(rid, NOW, {"new_videos": 1, "filed": {PL1: 1}, "deferred": 0, "errors": 2, "units": 51})
        assert s.recent_runs()[0]["summary"] == {
            "new_videos": 1, "filed": {PL1: 1}, "deferred": 0, "errors": 2, "units": 51}
    finally:
        s.close()


def test_cli_timer_run_waits_for_lock(gpath, yt, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TIMER_LOCK_WAIT", 5.0)
    release, holder = hold_lock_in_thread()
    threading.Timer(0.3, release.set).start()
    assert cli.main(["run", "--graph", str(gpath)]) == 0
    capsys.readouterr()
    holder.join(5)
    assert yt.inserts == [(PL1, "v1")]


def test_cli_timer_run_gives_up_after_wait(gpath, yt, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TIMER_LOCK_WAIT", 0.1)
    release, holder = hold_lock_in_thread()
    try:
        assert cli.main(["run", "--graph", str(gpath)]) == 3
    finally:
        release.set()
        holder.join(5)
    assert "another run is in progress" in capsys.readouterr().err.lower()
    assert yt.inserts == []


def test_run_now_auth_expired_is_error_not_exception(gpath):
    def boom():
        raise auth.AuthExpiredError("Google rejected the stored token")

    out = jobs.run_now("manual", make_client=boom, now=NOW)
    assert out.status == "error" and out.report is None
    assert "auth" in out.message.lower()


def test_run_now_network_down_is_error(gpath):
    def boom():
        raise httplib2.ServerNotFoundError("dns")

    out = jobs.run_now("manual", make_client=boom, now=NOW)
    assert out.status == "error" and "network" in out.message.lower()


def test_run_now_invalid_graph_is_error(tmp_path, monkeypatch, yt):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "nodes": [], "edges": [{"from": "a", "to": "b"}]}))
    monkeypatch.setattr(settings, "graph_path", bad)
    out = jobs.run_now("manual", make_client=lambda: (yt, yt.meter), now=NOW)
    assert out.status == "error" and "graph" in out.message.lower()
    assert runs() == []


def test_cli_run_uses_jobs(gpath, yt, monkeypatch, capsys):
    seen = []
    real = jobs.run_now

    def spy(trigger, **kwargs):
        seen.append(trigger)
        return real(trigger, **kwargs)

    monkeypatch.setattr(jobs, "run_now", spy)
    assert cli.main(["run", "--graph", str(gpath)]) == 0
    capsys.readouterr()
    assert seen == ["timer"] and runs()[0]["summary"]["trigger"] == "timer"

    with run_lock(settings.data_dir / "run.lock"):
        assert cli.main(["run", "--graph", str(gpath)]) == 3
    assert "another run is in progress" in capsys.readouterr().err.lower()

    def expired():
        raise auth.AuthExpiredError("Google rejected the stored token")

    monkeypatch.setattr(cli, "_client", expired)
    assert cli.main(["run", "--graph", str(gpath)]) == 2
    assert "expired or was revoked" in capsys.readouterr().err

    def offline():
        raise TimeoutError()

    monkeypatch.setattr(cli, "_client", offline)
    assert cli.main(["run", "--graph", str(gpath)]) == 4
    assert capsys.readouterr().err == "Error: network unavailable, try again later.\n"
    assert seen == ["timer"] * 4


# --- a fresh VM has no graph yet; the laptop can switch real runs off ---

def test_run_now_missing_graph_means_no_rules_yet(tmp_path, monkeypatch, yt):
    monkeypatch.setattr(settings, "graph_path", tmp_path / "missing.yaml")
    called = []
    out = jobs.run_now("manual", make_client=lambda: called.append(1) or (yt, yt.meter), now=NOW)
    assert (out.status, out.report, out.message) == ("done", None, jobs.NO_RULES)
    assert out.message == "No rules yet. Create them in the Rules tab."
    assert called == [] and runs() == []


def test_run_now_unreadable_graph_stays_an_error(tmp_path, monkeypatch, yt):
    monkeypatch.setattr(settings, "graph_path", tmp_path)  # a directory: exists but cannot be read as a file
    out = jobs.run_now("manual", make_client=lambda: (yt, yt.meter), now=NOW)
    assert out.status == "error" and "cannot read" in out.message.lower()


def test_cli_timer_run_without_graph_exits_0(tmp_path, yt, capsys):
    assert cli.main(["run", "--graph", str(tmp_path / "missing.yaml")]) == 0
    assert "No rules yet" in capsys.readouterr().out
    assert yt.inserts == [] and runs() == []


def test_cli_invalid_graph_stays_an_error(tmp_path, yt, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"version": 1, "nodes": [], "edges": [{"from": "a", "to": "b"}]}))
    assert cli.main(["run", "--graph", str(bad)]) == 1
    assert cli.main(["run", "--graph", str(tmp_path / "missing.yaml"), "--dry-run"]) == 1  # a dry run needs a graph
    capsys.readouterr()


def test_real_runs_disabled_exits_5(gpath, yt, monkeypatch, capsys):
    monkeypatch.setattr(settings, "real_runs_disabled", True)
    assert cli.main(["run", "--graph", str(gpath)]) == 5
    err = capsys.readouterr().err
    assert "REAL_RUNS_DISABLED" in err and len(err.strip().splitlines()) == 1
    assert yt.inserts == [] and runs() == []
    assert cli.main(["run", "--graph", str(gpath), "--dry-run"]) == 0  # dry runs still work
    capsys.readouterr()
    out = jobs.run_now("manual", make_client=lambda: (yt, yt.meter), now=NOW)  # "Check now" on the laptop too
    assert out.status == "error" and "REAL_RUNS_DISABLED" in out.message
    assert yt.inserts == [] and runs() == []
