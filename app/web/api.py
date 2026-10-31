"""JSON API for the web UI. Every route needs the owner; every non-GET route also passes the JSON-and-Origin check.

Handlers are plain functions, so FastAPI runs them in its threadpool. Each opens its own State (SQLite connections
stay on one thread) and charges the YouTube units it spends to today's quota log.
"""
import os
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app import jobs, queue
from app import run as runner
from app.config import Settings
from app.graph import Graph, GraphError, NotImplementedInThisVersion, parse_graph
from app.state import State

if TYPE_CHECKING:
    from app.web.server import Deps

PLAYLISTS_MAX_AGE = timedelta(seconds=60)


class BadRequest(Exception):
    """A request body the API cannot use; answered with 422 and this message."""


def owner(request: Request) -> str:
    return request.app.state.auth.owner(request)


def write_guard(request: Request, _: str = Depends(owner)) -> None:
    """Every method other than GET/HEAD must pass the JSON-and-Origin check, after the owner check."""
    if request.method not in ("GET", "HEAD"):
        request.app.state.auth.check_write(request)


def json_body(model: type[BaseModel]):
    """Dependency parsing the body as `model`, only after the owner and write checks have passed."""
    async def parse(request: Request, _: None = Depends(write_guard)) -> BaseModel:
        try:
            return model.model_validate(await request.json())
        except (ValueError, ValidationError):  # ValidationError is a ValueError; listed for the reader
            raise BadRequest("invalid request body") from None
    return parse


class TickBody(BaseModel):
    item_id: str
    video_id: str
    playlist_id: str


class QueueBody(BaseModel):
    playlist_id: str
    confirm_title: str


class GraphBody(BaseModel):
    graph: Any  # parse_graph reports what is wrong with it


def _write_atomic(path: Path, text: str) -> None:
    """Write next to the target and rename over it, so a crash leaves either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def build_router(cfg: Settings, deps: "Deps") -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(owner), Depends(write_guard)])
    # threadpool handlers share this; a race only costs an extra 1-unit fetch, and dict get/set are atomic in CPython
    playlists_cache: dict[str, tuple[datetime, dict[str, str]]] = {}

    @contextmanager
    def youtube(state: State, now: datetime) -> Iterator[Any]:
        """A YouTube client whose units are added to today's quota log, even when a call fails."""
        yt, meter = deps.make_client()
        before = meter.total
        try:
            yield yt
        finally:
            if meter.total > before:
                state.add_quota(runner.quota_day(now), meter.total - before)

    @contextmanager
    def open_state() -> Iterator[State]:
        state = State(cfg.data_dir / "state.db")
        try:
            yield state
        finally:
            state.close()

    def owned_playlists(yt: Any, now: datetime, fresh: bool = False) -> dict[str, str]:
        cached = playlists_cache.get("owned")
        if not fresh and cached and now - cached[0] < PLAYLISTS_MAX_AGE:
            return cached[1]
        owned = {p["id"]: p["snippet"]["title"] for p in yt.my_playlists()}
        playlists_cache["owned"] = (now, owned)
        return owned

    def check_graph(data: Any) -> tuple[Graph | None, list[str]]:
        """Parse the editor's graph; on success also check every destination is one of the owner's playlists."""
        try:
            graph = parse_graph(data)
        except GraphError as err:
            return None, err.errors
        if cfg.allow_any_playlist:
            return graph, []
        now = deps.now()
        with open_state() as state, youtube(state, now) as yt:
            owned = owned_playlists(yt, now, fresh=True)
        errors = [
            f"node {n.id!r}: playlist {n.params.playlist_id} is not one of your playlists"
            for n in graph.nodes.values()
            if n.type == "destination.playlist" and n.params.playlist_id not in owned
        ]
        return graph, errors

    @router.get("/me")
    def me(email: str = Depends(owner)) -> dict:
        return {"email": email}

    @router.get("/queue")
    def get_queue() -> dict:
        now = deps.now()
        with open_state() as state:
            playlist_id = queue.get_queue_playlist(state)
            if not playlist_id:
                return {"playlist": None, "items": []}
            with youtube(state, now) as yt:
                title = owned_playlists(yt, now).get(playlist_id, playlist_id)
                items = queue.list_queue(yt, state, now)
        return {"playlist": {"id": playlist_id, "title": title}, "items": [asdict(i) for i in items]}

    @router.post("/queue/tick")
    def tick(body: TickBody = Depends(json_body(TickBody))) -> Any:
        now = deps.now()
        with open_state() as state:
            yt, _meter = deps.make_client()  # queue.tick charges its own units
            try:
                return {"result": queue.tick(yt, state, now, body, cfg.tick_quota_ceiling)}
            except queue.QueueError as err:
                return JSONResponse({"error": str(err)}, 409)
            except queue.QuotaCeilingError as err:
                return JSONResponse({"error": str(err)}, 429)

    @router.get("/playlists")
    def playlists() -> list[dict]:
        now = deps.now()
        with open_state() as state, youtube(state, now) as yt:
            owned = owned_playlists(yt, now, fresh=True)
        return [{"id": pid, "title": title} for pid, title in owned.items()]

    @router.get("/settings")
    def get_settings() -> dict:
        with open_state() as state:
            return {"queue_playlist": queue.get_queue_playlist(state)}

    @router.put("/settings/queue")
    def put_queue(body: QueueBody = Depends(json_body(QueueBody))) -> Any:
        now = deps.now()
        with open_state() as state:
            with youtube(state, now) as yt:
                owned = owned_playlists(yt, now, fresh=True)
            try:
                queue.set_queue_playlist(state, body.playlist_id, owned, body.confirm_title, now)
            except queue.QueueError as err:
                return JSONResponse({"error": str(err)}, 422)
        return {"queue_playlist": body.playlist_id}

    @router.post("/run", status_code=202)
    def start_run() -> dict:
        status = jobs.start_background("manual", make_client=deps.make_client, settings=cfg)
        return {"status": status}

    @router.get("/activity")
    def activity() -> dict:
        now = deps.now()
        with open_state() as state:
            running = jobs.is_running(state, now)
            runs = state.recent_runs()
            used = state.quota_used(runner.quota_day(now))
        last = jobs.last_outcome()
        return {
            "running": running,
            "runs": runs,
            "quota": {"used_today": used, "budget": cfg.daily_quota_budget, "tick_ceiling": cfg.tick_quota_ceiling},
            "last_manual": last and {
                "status": last.status, "message": last.message,
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            },
        }

    @router.get("/graph")
    def get_graph() -> Any:
        try:
            text = cfg.graph_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"graph": None}
        try:
            return {"graph": yaml.safe_load(text)}
        except yaml.YAMLError:
            return JSONResponse({"error": "The saved graph file is not valid YAML."}, 500)

    @router.put("/graph")
    def put_graph(body: GraphBody = Depends(json_body(GraphBody))) -> Any:
        graph, errors = check_graph(body.graph)
        if errors:
            return JSONResponse({"errors": errors}, 422)
        try:
            _write_atomic(cfg.graph_path, yaml.safe_dump(body.graph, sort_keys=False, allow_unicode=True))
        except OSError:
            return JSONResponse({"error": "Could not save the graph file."}, 500)
        return {"ok": True, "warnings": graph.warnings}

    @router.post("/graph/preview")
    def preview(body: GraphBody = Depends(json_body(GraphBody))) -> Any:
        try:
            graph = parse_graph(body.graph)
        except GraphError as err:
            return JSONResponse({"errors": err.errors}, 422)
        yt, meter = deps.make_client()
        state = State.throwaway_copy(cfg.data_dir / "state.db")  # writes stay in memory
        try:
            report = runner.run(
                graph, yt, state, meter, deps.now(), cfg.daily_quota_budget,
                allow_any_playlist=cfg.allow_any_playlist, dry_run=True, fetch=deps.fetch,
            )
        except NotImplementedInThisVersion as err:
            return JSONResponse({"errors": [str(err)]}, 422)
        finally:
            state.close()
        return {"report": runner.render(report, graph)}

    return router
