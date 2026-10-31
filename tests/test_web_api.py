"""The JSON API behind the web UI: queue and ticks, runs and activity, settings, and the rule graph editor."""
import hashlib
import logging
import os
import threading

import pytest
import yaml

from app import auth, cli, jobs, queue, secretstore
from app.config import settings
from app.graph import load_graph
from app.state import State
from tests.test_detect import CH
from tests.test_queue import PLA, PLB, pl_item
from tests.test_run import DAY, NOW, PL1, OTHER
from tests.test_web_auth import API_ROUTES, LOGIN_SECRET, WRITE, call, make_env
from tests.test_web_auth import api, env, web_settings  # noqa: F401 - fixtures


def db():
    return State(settings.data_dir / "state.db")


def with_queue(e, playlist_id=PLB, n=2):
    e.yt.owned.append(playlist_id)
    e.yt.queues[playlist_id] = [
        pl_item(f"item{i}", f"v{i}", playlist_id, f"2026-10-2{i}T12:00:00Z") for i in range(1, n + 1)
    ]
    s = db()
    queue.set_queue_playlist(s, playlist_id, {playlist_id: "Queue"}, "Queue", NOW)
    s.close()


def tick(c, item_id, video_id, playlist_id=PLB):
    return c.post("/api/queue/tick", json={"item_id": item_id, "video_id": video_id, "playlist_id": playlist_id},
                  headers=WRITE)


def graph_doc(*dests, extra_nodes=(), extra_edges=()):
    nodes = [{"id": "s", "type": "source.channel", "params": {"channel_id": CH}, "ui": {"x": 0, "y": 0}}]
    edges = []
    for i, pl in enumerate(dests or (PL1,)):
        nodes.append({"id": f"d{i}", "type": "destination.playlist", "params": {"playlist_id": pl, "title": "Mine"}})
        edges.append({"from": "s", "to": f"d{i}"})
    return {"version": 1, "settings": {"multi_match": "all"}, "nodes": nodes + list(extra_nodes),
            "edges": edges + list(extra_edges)}


@pytest.fixture
def saved_graph():
    settings.graph_path.parent.mkdir(parents=True, exist_ok=True)
    original = "# hand-written\n" + yaml.safe_dump(graph_doc(), sort_keys=False)
    settings.graph_path.write_text(original, encoding="utf-8", newline="\n")
    return settings.graph_path.read_bytes()


# --- queue ---

def test_queue_empty_when_unset(api):
    assert api.client.get("/api/queue").json() == {"playlist": None, "items": []}


def test_queue_and_tick_happy_path(api):
    with_queue(api)
    c = api.client
    body = c.get("/api/queue").json()
    assert body["playlist"] == {"id": PLB, "title": f"Title {PLB}"}
    assert [i["item_id"] for i in body["items"]] == ["item2", "item1"]  # newest added first
    first = body["items"][0]
    assert first["video_id"] == "v2" and first["playlist_id"] == PLB and first["channel"] == "Some Channel"
    assert first["thumbnail"].endswith("/mqdefault.jpg") and first["duration_seconds"] == 600
    r = tick(c, "item1", "v1")
    assert (r.status_code, r.json()) == (200, {"result": "removed"})
    assert api.yt.deletes == ["item1"]
    assert [i["item_id"] for i in c.get("/api/queue").json()["items"]] == ["item2"]
    assert tick(c, "item1", "v1").json() == {"result": "already_gone"}
    assert api.yt.deletes == ["item1"]
    s = db()
    assert s.quota_used(DAY) >= 50  # the delete and the reads are charged
    s.close()


def test_tick_queue_changed_409(api):
    with_queue(api)
    r = tick(api.client, "item1", "v1", playlist_id=PLA)
    assert (r.status_code, r.json()) == (409, {"error": "queue changed, refresh"})
    r = tick(api.client, "item1", "v2")  # the item now holds another video
    assert r.status_code == 409
    assert api.yt.deletes == [] and "delete" not in api.yt.calls


def test_tick_ceiling_429(api):
    with_queue(api)
    s = db()
    s.add_quota(DAY, 9460)
    s.close()
    r = tick(api.client, "item1", "v1")
    assert r.status_code == 429 and "quota" in r.json()["error"]
    assert api.yt.deletes == []


def test_tick_body_validated(api):
    r = api.client.post("/api/queue/tick", json={"item_id": "item1"}, headers=WRITE)
    assert (r.status_code, r.json()) == (422, {"error": "invalid request body"})
    r = api.client.post("/api/queue/tick", content=b"{not json", headers={**WRITE, "Content-Type": "application/json"})
    assert r.status_code == 422


# --- runs and activity ---

def test_run_twice_202_already_running(api, saved_graph):
    entered, release = threading.Event(), threading.Event()

    def blocking_client():
        entered.set()
        release.wait(5)
        return api.yt, api.yt.meter

    api.deps.make_client = blocking_client
    r = api.client.post("/api/run", json={}, headers=WRITE)
    assert (r.status_code, r.json()) == (202, {"status": "started"})
    assert entered.wait(5)
    r = api.client.post("/api/run", json={}, headers=WRITE)
    assert (r.status_code, r.json()) == (202, {"status": "already_running"})
    assert api.client.get("/api/activity").json()["running"] is True
    release.set()
    jobs._thread.join(5)
    act = api.client.get("/api/activity").json()
    assert act["running"] is False and len(act["runs"]) == 1
    assert act["runs"][0]["summary"]["trigger"] == "manual"
    assert act["last_manual"]["status"] == "done" and "Filed" in act["last_manual"]["message"]


def test_activity_shape(api):
    s = db()
    run_id = s.start_run(NOW)
    s.finish_run(run_id, NOW, {"trigger": "timer", "errors": 2, "units": 40})  # an older row shape passes through
    s.add_quota(DAY, 123)
    s.close()
    act = api.client.get("/api/activity").json()
    assert set(act) == {"running", "runs", "quota", "last_manual"}
    assert act["running"] is False and act["last_manual"] is None
    assert act["quota"] == {"used_today": 123, "budget": settings.daily_quota_budget, "tick_ceiling": 9500}
    (row,) = act["runs"]
    assert row["summary"] == {"trigger": "timer", "errors": 2, "units": 40} and row["finished_at"]


# --- settings and playlists ---

def test_settings_queue_requires_typed_title(api):
    api.yt.owned.append(PLB)
    c = api.client
    assert c.get("/api/playlists").json() == [{"id": p, "title": f"Title {p}"} for p in api.yt.owned]
    r = c.put("/api/settings/queue", json={"playlist_id": PLB, "confirm_title": "title plb"}, headers=WRITE)
    assert r.status_code == 422 and "title" in r.json()["error"]
    r = c.put("/api/settings/queue", json={"playlist_id": OTHER, "confirm_title": f"Title {OTHER}"}, headers=WRITE)
    assert r.status_code == 422 and "isn't one of yours" in r.json()["error"]
    assert c.get("/api/settings").json() == {"queue_playlist": None}
    r = c.put("/api/settings/queue", json={"playlist_id": PLB, "confirm_title": f"Title {PLB}"}, headers=WRITE)
    assert (r.status_code, r.json()) == (200, {"queue_playlist": PLB})
    assert c.get("/api/settings").json() == {"queue_playlist": PLB}


# --- graph ---

def test_get_graph(api, saved_graph):
    assert api.client.get("/api/graph").json() == {"graph": graph_doc()}


def test_get_graph_missing_file(api):
    assert api.client.get("/api/graph").json() == {"graph": None}


@pytest.mark.parametrize("bad, named", [
    (graph_doc(extra_edges=[{"from": "s", "to": "ghost"}]), "'ghost'"),
    (graph_doc(OTHER), "'d0'"),
    (graph_doc(extra_nodes=[{"id": "odd", "type": "filter.nope"}]), "'odd'"),
    ([1, 2], "mapping"),
])
def test_put_graph_invalid_422_unchanged(api, saved_graph, bad, named):
    r = api.client.put("/api/graph", json={"graph": bad}, headers=WRITE)
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert errors and any(named in e for e in errors), errors
    assert settings.graph_path.read_bytes() == saved_graph
    assert [p.name for p in settings.graph_path.parent.iterdir()] == ["mine.yaml"]


def test_put_graph_valid_writes_atomically(api, saved_graph, monkeypatch):
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((os.path.dirname(src), os.fspath(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    doc = graph_doc(PL1, "PLaaaaaaaaaa2")
    r = api.client.put("/api/graph", json={"graph": doc}, headers=WRITE)
    assert (r.status_code, r.json()) == (200, {"ok": True, "warnings": []})
    assert replaced == [(str(settings.graph_path.parent), str(settings.graph_path))]
    assert yaml.safe_load(settings.graph_path.read_text(encoding="utf-8")) == doc
    assert set(load_graph(settings.graph_path).nodes) == {"s", "d0", "d1"}
    assert api.client.get("/api/graph").json() == {"graph": doc}
    assert [p.name for p in settings.graph_path.parent.iterdir()] == ["mine.yaml"]


def test_put_graph_failed_write_keeps_old_file(api, saved_graph, monkeypatch):
    def broken(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", broken)
    r = api.client.put("/api/graph", json={"graph": graph_doc()}, headers=WRITE)
    assert (r.status_code, r.json()) == (500, {"error": "Could not save the graph file."})
    assert settings.graph_path.read_bytes() == saved_graph
    assert [p.name for p in settings.graph_path.parent.iterdir()] == ["mine.yaml"]


def test_preview_writes_nothing(api, saved_graph):
    s = db()
    s.mark_channel_known(CH, NOW)  # not a first run, so the preview would file v1
    s.close()
    state_file = settings.data_dir / "state.db"
    before = hashlib.sha256(state_file.read_bytes()).hexdigest()
    r = api.client.post("/api/graph/preview", json={"graph": graph_doc()}, headers=WRITE)
    assert r.status_code == 200
    report = r.json()["report"]
    assert "DRY RUN - nothing written. New videos: 1" in report and f"Title {PL1}: 1" in report
    assert hashlib.sha256(state_file.read_bytes()).hexdigest() == before
    assert settings.graph_path.read_bytes() == saved_graph
    assert api.yt.inserts == []
    r = api.client.post("/api/graph/preview", json={"graph": graph_doc(extra_nodes=[{"id": "x", "type": "nope"}])},
                        headers=WRITE)
    assert r.status_code == 422 and any("'x'" in e for e in r.json()["errors"])


def test_preview_reserved_node_is_422(api):
    doc = graph_doc(extra_nodes=[{"id": "llm", "type": "filter.llm", "params": {"prompt": "x"}}],
                    extra_edges=[{"from": "s", "to": "llm"}, {"from": "llm", "to": "d0"}])
    r = api.client.post("/api/graph/preview", json={"graph": doc}, headers=WRITE)
    assert r.status_code == 422 and "not implemented" in r.json()["errors"][0]


# --- static UI ---

def test_static_ui(api, tmp_path):
    r = api.client.get("/")
    assert (r.status_code, r.text) == (503, "UI not built")
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>ytps</title>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    assert "<title>ytps</title>" in api.client.get("/").text
    assert "<title>ytps</title>" in api.client.get("/rules").text  # client-side route
    assert api.client.get("/assets/app.js").text == "console.log(1)"
    assert api.client.get("/api/unknown").status_code == 404


# --- content security policy ---

CSP = (
    "default-src 'self'; "
    "img-src 'self' https://i.ytimg.com https://*.ytimg.com https://*.ggpht.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self' https://accounts.google.com"
)


def test_csp_header_on_ui_api_and_errors(api, tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>ytps</title>", encoding="utf-8")

    def broken():
        raise RuntimeError("boom")

    responses = [api.client.get("/"), api.client.get("/rules"), api.client.get("/api/me"),
                 api.client.get("/api/unknown"), api.client.get("/healthz")]
    api.deps.make_client = broken
    responses.append(api.client.get("/api/playlists"))  # the bare 500
    responses.append(api.client.post("/auth/logout", json={}, headers=WRITE))
    responses.append(api.client.get("/api/me"))  # signed out: 401
    assert [r.status_code for r in responses] == [200, 200, 200, 404, 200, 500, 200, 401]
    for r in responses:
        assert r.headers["content-security-policy"] == CSP, r.request.url


def test_csp_header_on_sign_in_pages(env):
    assert env.client.get("/").headers["content-security-policy"] == CSP  # the redirect to sign-in
    assert env.client.get("/auth/callback").headers["content-security-policy"] == CSP  # the refused page


# --- errors and secrets ---

def test_unexpected_error_is_short_and_logged_by_type(api, caplog):
    def broken():
        raise RuntimeError("detail that must stay out of the response")

    api.deps.make_client = broken
    with caplog.at_level(logging.ERROR):
        r = api.client.get("/api/playlists")
    assert (r.status_code, r.json()) == (500, {"error": "internal error"})
    assert "RuntimeError" in caplog.text and "detail that must" not in caplog.text


def test_youtube_auth_expired_is_503(api):
    def expired():
        raise auth.AuthExpiredError("Google rejected the stored token")

    api.deps.make_client = expired
    r = api.client.get("/api/playlists")
    assert r.status_code == 503 and "sign" in r.json()["error"].lower()


def test_no_secret_in_any_response(web_settings, tmp_path, caplog):
    markers = {"refresh-token": "REFRESH-TOKEN-MARKER-91c2", "session-key": "SESSION-KEY-MARKER-55d0e6a1b2c3d4e5"}
    for name, value in markers.items():
        secretstore.set(name, value)
    secrets = [LOGIN_SECRET, *markers.values()]
    e = make_env(tmp_path)
    responses = []
    with caplog.at_level(logging.DEBUG), e.client as c:
        responses += [c.get("/healthz"), c.get("/"), c.get("/api/me")]
        responses += [call(c, m, p) for m, p in API_ROUTES]  # signed out
        r = c.get("/auth/login")
        responses.append(r)
        state = r.headers["location"].split("state=")[1].split("&")[0]
        responses.append(c.get("/auth/callback", params={"code": "c", "state": "bad"}))
        responses.append(c.get("/auth/callback", params={"code": "c", "state": state}))
        with_queue(e)
        bodies = {
            "/api/queue/tick": {"item_id": "item1", "video_id": "v1", "playlist_id": PLB},
            "/api/settings/queue": {"playlist_id": PLB, "confirm_title": "wrong"},
            "/api/graph": {"graph": graph_doc(OTHER)},
            "/api/graph/preview": {"graph": graph_doc()},
        }
        for method, path in API_ROUTES:
            if path != "/api/run":
                responses.append(call(c, method, path, json=bodies.get(path, {})))
        responses.append(c.post("/api/queue/tick", json={}, headers=WRITE))
        responses.append(c.post("/auth/logout", json={}, headers=WRITE))
    assert len(responses) > 25
    for r in responses:
        blob = r.text + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for secret in secrets:
            assert secret not in blob, (r.request.url, secret)
    for secret in secrets:
        assert secret not in caplog.text


# --- serve command ---

def test_serve_binds_loopback_only(web_settings, monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    monkeypatch.setattr(settings, "bind_port", 8999)
    assert cli.main(["serve"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8999
    assert seen["app"].title  # a FastAPI app
