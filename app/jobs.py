"""One run entry point shared by the timer (CLI `run`) and the UI's "Check now"; both hold the run lock."""
import logging
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
LOCK_RETRY_SECONDS = 1.0
STALE_RUN_AFTER = timedelta(hours=2)  # an unfinished runs row older than this was left by a crash
NO_RULES = "No rules yet. Create them in the Rules tab."
REAL_RUNS_DISABLED = "Real runs are off on this machine (REAL_RUNS_DISABLED); use --dry-run here."


@dataclass
class RunOutcome:
    status: Literal["done", "already_running", "error"]
    report: runner.Report | None
    message: str
    error: Exception | None = None  # the original failure, so the CLI can keep its exit codes
    trigger: Trigger | None = None  # set on background outcomes kept for last_outcome()
    finished_at: datetime | None = None


def real_client() -> tuple[RealYouTube, QuotaMeter]:
    meter = QuotaMeter()
    return RealYouTube.from_credentials(auth.load_credentials(), meter), meter


@contextmanager
def _locked(path: Path, wait: float):
    """Take the run lock, retrying for up to `wait` seconds; raise AlreadyRunning after that."""
    deadline = time.monotonic() + wait
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(run_lock(path))
                break
            except AlreadyRunning:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(LOCK_RETRY_SECONDS)
        yield


def run_now(
    trigger: Trigger,
    *,
    graph: Graph | None = None,
    make_client: Callable[[], tuple[object, QuotaMeter]] = real_client,
    now: datetime | None = None,
    settings: Settings | None = None,
    backfill: int = 0,
    wait: float = 0,
) -> RunOutcome:
    """One real run under the lock, waiting up to `wait` seconds for it.

    Never raises for expected failures; they come back as status "error".
    """
    cfg = settings or default_settings
    if cfg.real_runs_disabled:
        return RunOutcome("error", None, REAL_RUNS_DISABLED)
    if graph is None:
        if not cfg.graph_path.exists():
            return RunOutcome("done", None, NO_RULES)  # a fresh server: nothing to run until rules are saved
        try:
            graph = load_graph(cfg.graph_path)
        except GraphError as err:
            return RunOutcome("error", None, "Graph is invalid: " + "; ".join(err.errors), err)
        except OSError as err:
            return RunOutcome("error", None, f"Cannot read graph file {cfg.graph_path}: {err.strerror or type(err).__name__}", err)
    try:
        with _locked(cfg.data_dir / "run.lock", wait):
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
_last: RunOutcome | None = None
_last_guard = threading.Lock()


def _background(trigger: Trigger, **kwargs) -> None:
    global _last
    outcome = run_now(trigger, **kwargs)
    with _last_guard:
        _last = replace(outcome, trigger=trigger, finished_at=datetime.now(timezone.utc))


def last_outcome() -> RunOutcome | None:
    """Outcome of the most recent background run in this process, including failures and lost races."""
    with _last_guard:
        return _last


def start_background(
    trigger: Trigger = "manual",
    *,
    graph: Graph | None = None,
    backfill: int = 0,
    make_client: Callable[[], tuple[object, QuotaMeter]] = real_client,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Literal["started", "already_running"]:
    """Start run_now in a daemon thread unless this process's previous one is still going.

    The run lock is not probed here: probing would briefly hold it and could make a timer run exit. If the timer
    holds the lock, the thread's run_now returns already_running and last_outcome() records it.
    """
    global _thread
    kwargs = dict(graph=graph, backfill=backfill, make_client=make_client, now=now, settings=settings)
    with _start_guard:
        if _thread is not None and _thread.is_alive():
            return "already_running"
        _thread = threading.Thread(target=_background, args=(trigger,), kwargs=kwargs, daemon=True, name="run-now")
        _thread.start()
    return "started"


def is_running(state: State, now: datetime | None = None) -> bool:
    """True while our thread runs or the newest runs row is unfinished and recent (covers timer runs). No lock probe."""
    if _thread is not None and _thread.is_alive():
        return True
    rows = state.recent_runs(limit=1)
    if not rows or rows[0]["finished_at"] is not None:
        return False
    started = datetime.fromisoformat(rows[0]["started_at"])
    return (now or datetime.now(timezone.utc)) - started < STALE_RUN_AFTER
