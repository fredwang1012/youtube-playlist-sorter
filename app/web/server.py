"""FastAPI app factory: auth routes, the JSON API, /healthz and the prebuilt UI from app/web/static/.

Only /auth/* and /healthz are public. Errors reach the client as short messages; unexpected ones are logged by
exception type only, since a message or traceback could carry request data.
"""
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httplib2
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from google.auth.exceptions import TransportError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import auth as youtube_auth
from app import jobs
from app import run as runner
from app.config import Settings
from app.rss import Fetcher
from app.secretstore import SecretStoreError
from app.web import api
from app.web.auth import Auth, Forbidden, NotSignedIn, google_verify
from app.youtube import QuotaExceededError, QuotaMeter, YouTubeError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Deps:
    """What the app reaches outside itself; tests swap in fakes."""
    make_client: Callable[[], tuple[Any, QuotaMeter]] = jobs.real_client
    now: Callable[[], datetime] = _utc_now
    token_transport: httpx.BaseTransport | None = None  # None: the real Google token endpoint
    verify_id_token: Callable[[str, str], dict[str, Any]] = google_verify
    fetch: Fetcher = runner.fetch_url
    static_dir: Path = STATIC_DIR


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/") or request.url.path == "/api"


def _error(request: Request, status: int, message: str) -> Response:
    if _is_api(request):
        return JSONResponse({"error": message}, status)
    return PlainTextResponse(message, status)


class _ShortErrors:
    """Turn an unhandled exception into a bare 500, logging only its type."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception as err:
            log.error("%s %s failed: %s", scope["method"], scope["path"], type(err).__name__)
            if not started:
                await JSONResponse({"error": "internal error"}, 500)(scope, receive, send)


# the built UI loads only its own script and stylesheet; thumbnails come from YouTube's image hosts, and the only
# form target outside this origin is Google's sign-in page
CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "img-src 'self' https://i.ytimg.com https://*.ytimg.com https://*.ggpht.com",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self' https://accounts.google.com",
))


class _SecurityHeaders:
    """Add the Content-Security-Policy header to every HTTP response, error pages included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"content-security-policy"]
                headers.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)


def create_app(settings: Settings, deps: Deps | None = None) -> FastAPI:
    deps = deps or Deps()
    auth = Auth(settings, lambda: deps.now(), deps.verify_id_token, deps.token_transport)
    app = FastAPI(title="youtube-playlist-sorter", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.add_middleware(_ShortErrors)
    app.add_middleware(_SecurityHeaders)  # added last, so it wraps the bare 500 from _ShortErrors too

    @app.exception_handler(NotSignedIn)
    def not_signed_in(request: Request, _err: NotSignedIn) -> Response:
        if _is_api(request):
            return JSONResponse({"error": "login required"}, 401)
        if settings.auth_mode == "tailscale":
            return PlainTextResponse("Open this page from the owner's Tailscale device.", 401)
        return RedirectResponse("/auth/login", 302)

    @app.exception_handler(Forbidden)
    def forbidden(request: Request, err: Forbidden) -> Response:
        return _error(request, 403, str(err))

    @app.exception_handler(api.BadRequest)
    def bad_request(request: Request, err: api.BadRequest) -> Response:
        return _error(request, 422, str(err))

    @app.exception_handler(RequestValidationError)
    def invalid(request: Request, _err: RequestValidationError) -> Response:
        return _error(request, 422, "invalid request")

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, err: StarletteHTTPException) -> Response:
        return _error(request, err.status_code, "not found" if err.status_code == 404 else "request refused")

    @app.exception_handler(youtube_auth.AuthError)
    def youtube_signed_out(request: Request, err: youtube_auth.AuthError) -> Response:
        log.warning("YouTube auth: %s", type(err).__name__)
        return _error(request, 503, "YouTube access needs signing in again: run the auth command on the server.")

    @app.exception_handler(SecretStoreError)
    def secret_store(request: Request, err: SecretStoreError) -> Response:
        log.error("Secret store: %s", err)  # its messages say how to fix it and never hold a secret
        return _error(request, 503, "The server's secret store is not set up.")

    @app.exception_handler(QuotaExceededError)
    def quota(request: Request, _err: QuotaExceededError) -> Response:
        return _error(request, 429, "YouTube quota exceeded for today; it resets at midnight Pacific time.")

    @app.exception_handler(YouTubeError)
    def youtube_error(request: Request, err: YouTubeError) -> Response:
        return _error(request, 502, str(err))  # YouTubeError messages never include response bodies

    for network_error in (TransportError, httplib2.ServerNotFoundError, ConnectionError, TimeoutError, socket.gaierror):
        app.add_exception_handler(
            network_error, lambda request, _err: _error(request, 503, "Network unavailable, try again later.")
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    app.include_router(auth.router())
    app.include_router(api.build_router(settings, deps))

    @app.get("/{path:path}", include_in_schema=False)
    def ui(request: Request, path: str, _owner: str = Depends(api.owner)) -> Response:
        if path.split("/")[0] in ("api", "auth"):
            return _error(request, 404, "not found")
        root = deps.static_dir.resolve()
        index = root / "index.html"
        if not index.is_file():
            return PlainTextResponse("UI not built", 503)
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            return FileResponse(target)
        return FileResponse(index)  # client-side routes

    return app
