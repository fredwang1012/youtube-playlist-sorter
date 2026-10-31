"""Shared run entry point: the timer (CLI) and "Check now" both go through jobs.run_now under the run lock."""
import json
import threading

import httplib2
import pytest

from app import auth, cli, jobs, run as runner
from app.config import settings
from app.lock import run_lock
from app.state import State
from tests.test_detect import CH
from tests.test_run import NOW, PL1, FakeYT, fetcher


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


def test_start_background_refuses_while_timer_holds_lock(gpath, yt):
    with run_lock(settings.data_dir / "run.lock"):
        assert jobs.start_background("manual", make_client=lambda: (yt, yt.meter), now=NOW) == "already_running"
    assert runs() == []


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
