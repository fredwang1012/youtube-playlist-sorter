"""One run entry point shared by the timer (CLI `run`) and the UI's "Check now"; both hold the run lock."""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

import httplib2
from google.auth.exceptions import TransportError

from app import auth
from app import run as runner
from app.config import Settings
from app.config import settings as default_settings
from app.graph import Graph, GraphError, NotImplementedInThisVersion, load_graph
from app.lock import AlreadyRunning, run_lock
from app.state import State
from app.youtube import QuotaMeter, RealYouTube, YouTubeError

log = logging.getLogger(__name__)

Trigger = Literal["timer", "manual"]
NETWORK_ERRORS = (TransportError, httplib2.ServerNotFoundError, OSError)  # OSError covers timeouts and resets


@dataclass
class RunOutcome:
    status: Literal["done", "already_running", "error"]
    report: runner.Report | None
    message: str
    error: Exception | None = None  # the original failure, so the CLI can keep its exit codes


def real_client() -> tuple[RealYouTube, QuotaMeter]:
    meter = QuotaMeter()
    return RealYouTube.from_credentials(auth.load_credentials(), meter), meter


def run_now(
    trigger: Trigger,
    *,
    graph: Graph | None = None,
    make_client: Callable[[], tuple[object, QuotaMeter]] = real_client,
    now: datetime | None = None,
    settings: Settings | None = None,
    backfill: int = 0,
) -> RunOutcome:
    """One real run under the lock. Never raises for expected failures; they come back as status "error"."""
    cfg = settings or default_settings
    if graph is None:
        try:
            graph = load_graph(cfg.graph_path)
        except GraphError as err:
            return RunOutcome("error", None, "Graph is invalid: " + "; ".join(err.errors), err)
        except OSError as err:
            return RunOutcome("error", None, f"Cannot read graph file {cfg.graph_path}: {err.strerror or type(err).__name__}", err)
    try:
        with run_lock(cfg.data_dir / "run.lock"):
            yt, meter = make_client()
            state = State(cfg.data_dir / "state.db")
            try:
                report = runner.run(
                    graph, yt, state, meter, now or datetime.now(timezone.utc), cfg.daily_quota_budget,
                    allow_any_playlist=cfg.allow_any_playlist, fetch=runner.fetch_url, backfill=backfill,
                    trigger=trigger,
                )
            finally:
                state.close()
    except AlreadyRunning as err:
        return RunOutcome("already_running", None, "Another run is in progress.", err)
    except auth.AuthExpiredError as err:
        return RunOutcome("error", None, "Google auth expired or was revoked; sign in again.", err)
    except auth.AuthError as err:
        return RunOutcome("error", None, f"Google auth failed: {err}", err)
    except (YouTubeError, NotImplementedInThisVersion) as err:
        return RunOutcome("error", None, str(err), err)
    except NETWORK_ERRORS as err:
        return RunOutcome("error", None, "Network unavailable, try again later.", err)
    except Exception as err:  # a background run has no caller to report to
        log.exception("run failed")
        return RunOutcome("error", None, f"Run failed: {type(err).__name__}", err)
    filed = sum(report.filed.values())
    return RunOutcome("done", report, f"Filed {filed} video(s), {len(report.errors) + len(report.config_errors)} error(s).")


_thread: threading.Thread | None = None
_start_guard = threading.Lock()


def start_background(trigger: Trigger = "manual", **kwargs) -> Literal["started", "already_running"]:
    """Start run_now in a daemon thread unless a run (this process's or the timer's) is already going."""
    global _thread
    cfg = kwargs.get("settings") or default_settings
    with _start_guard:
        if _thread is not None and _thread.is_alive():
            return "already_running"
        try:
            with run_lock(cfg.data_dir / "run.lock"):
                pass
        except AlreadyRunning:
            return "already_running"
        # a timer run could still take the lock before the thread does; run_now then reports already_running
        _thread = threading.Thread(target=run_now, args=(trigger,), kwargs=kwargs, daemon=True, name="run-now")
        _thread.start()
    return "started"
