"""Owner-only sign-in: Google OpenID Connect with PKCE, or the Tailscale identity header as a fallback.

The tailnet is shared with another account, so reaching the server proves nothing. Google mode accepts only a
verified owner_email; Tailscale mode accepts only Tailscale-User-Login == owner_tailscale_login. Sessions are a signed
cookie holding the email, the sign-in time and the session epoch; logout bumps the epoch in State, which signs out
every device. Tailscale mode has no sessions: each request carries the identity header, so there is nothing to revoke.
"""
import base64
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from itsdangerous import BadSignature, URLSafeSerializer

from app import secretstore
from app.config import Settings
from app.state import State

log = logging.getLogger(__name__)

SESSION_COOKIE = "ytps_session"
OAUTH_COOKIE = "ytps_oauth"  # state and PKCE verifier between /auth/login and /auth/callback
SESSION_MAX_AGE = timedelta(days=30)
OAUTH_MAX_AGE = timedelta(minutes=10)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
TAILSCALE_LOGIN_HEADER = "Tailscale-User-Login"
LOGIN_CLIENT_SECRET = "login-client-secret"
SESSION_KEY = "session-key"
EPOCH_KEY = "session_epoch"  # State kv; a session signed under an older epoch is refused

REFUSED_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Sign-in refused</title>"
    "<p>Sign-in refused. Only the owner's Google account can use this page.</p>"
    '<p><a href="/auth/login">Try again</a></p>'
)


class NotSignedIn(Exception):
    """No valid owner credential: 401 for the API, the login page for HTML."""


class Forbidden(Exception):
    """A credential or request that is never allowed; the message is shown to the client."""


class LoginRefused(Exception):
    """The Google callback failed a check; the message is logged, never shown."""


def google_verify(token: str, audience: str) -> dict[str, Any]:
    """Check the ID token's signature, issuer, expiry and audience against Google's published keys."""
    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


def _session_key() -> str:
    key = secretstore.get(SESSION_KEY)
    if not key:
        # locally the key is made once and kept in the keychain; the systemd backend raises with setup steps
        key = secrets.token_urlsafe(32)
        secretstore.set(SESSION_KEY, key)
    return key


def _same(a: str, b: str) -> bool:
    # compare_digest raises TypeError on non-ASCII str, so compare the UTF-8 bytes
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class Auth:
    cfg: Settings
    now: Callable[[], datetime]
    verify_id_token: Callable[[str, str], dict[str, Any]]
    token_transport: httpx.BaseTransport | None = None

    def __post_init__(self) -> None:
        if self.cfg.auth_mode == "google":
            if not self.cfg.owner_email.strip():
                raise ValueError("OWNER_EMAIL must be set for Google sign-in.")
            key = _session_key()
            self._sessions = URLSafeSerializer(key, salt="ytps-session")
            self._oauth = URLSafeSerializer(key, salt="ytps-oauth")

    def _epoch(self) -> int:
        state = State(self.cfg.data_dir / "state.db")
        try:
            row = state.kv_get(EPOCH_KEY)
        finally:
            state.close()
        return int(row[0]) if row else 0

    def _bump_epoch(self) -> None:
        state = State(self.cfg.data_dir / "state.db")
        try:
            row = state.kv_get(EPOCH_KEY)
            state.kv_set(EPOCH_KEY, str((int(row[0]) if row else 0) + 1), self.now())
        finally:
            state.close()

    @property
    def redirect_uri(self) -> str:
        return self.cfg.public_url.rstrip("/") + "/auth/callback"

    def _cookie(self, response: Response, name: str, value: str, max_age: timedelta, path: str) -> None:
        response.set_cookie(name, value, max_age=int(max_age.total_seconds()), path=path,
                            httponly=True, secure=True, samesite="lax")

    def _signed(self, serializer: URLSafeSerializer, value: str | None, max_age: timedelta) -> dict | None:
        """The payload of a signed cookie issued less than max_age ago, else None."""
        if not value:
            return None
        try:
            data = serializer.loads(value)
        except BadSignature:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("iat"), int):
            return None
        age = self.now().timestamp() - data["iat"]
        return data if -60 <= age <= max_age.total_seconds() else None

    # --- request checks (used as FastAPI dependencies by the API) ---

    def owner(self, request: Request) -> str:
        """The owner's identity, or NotSignedIn / Forbidden."""
        # a browser marks requests from other sites; refusing them for the API stops a page elsewhere spending quota
        if request.url.path.startswith("/api") and request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
            raise Forbidden("cross-site request")
        if self.cfg.auth_mode == "tailscale":
            login = request.headers.get(TAILSCALE_LOGIN_HEADER, "")
            if not login:
                raise NotSignedIn()
            if not self.cfg.owner_tailscale_login or not _same(login, self.cfg.owner_tailscale_login):
                raise Forbidden("not the owner")
            return login
        data = self._signed(self._sessions, request.cookies.get(SESSION_COOKIE), SESSION_MAX_AGE)
        email = data.get("email") if data else None
        if not isinstance(email, str) or email.casefold() != self.cfg.owner_email.casefold():
            raise NotSignedIn()
        if data.get("ep") != self._epoch():
            raise NotSignedIn()
        return email

    def check_write(self, request: Request) -> None:
        """POST and PUT must be JSON from our own origin, which a cross-site form or script cannot send."""
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Forbidden("JSON body required")
        if request.headers.get("origin") != self.cfg.public_url.rstrip("/"):
            raise Forbidden("bad origin")

    # --- routes ---

    def router(self) -> APIRouter:
        router = APIRouter(prefix="/auth")

        @router.get("/login")
        def login() -> Response:
            if self.cfg.auth_mode == "tailscale":
                return RedirectResponse("/", 302)
            if not self.cfg.login_client_id:
                return HTMLResponse("Google sign-in is not configured.", 503)
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
            params = {
                "client_id": self.cfg.login_client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
            }
            response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", 302)
            cookie = self._oauth.dumps({"state": state, "verifier": verifier, "iat": int(self.now().timestamp())})
            self._cookie(response, OAUTH_COOKIE, cookie, OAUTH_MAX_AGE, "/auth")
            return response

        @router.get("/callback")
        def callback(request: Request, code: str = "", state: str = "") -> Response:
            try:
                email = self._finish_login(request.cookies.get(OAUTH_COOKIE), code, state)
            except LoginRefused as err:
                log.warning("Sign-in refused: %s", err)
                response: Response = HTMLResponse(REFUSED_PAGE, 403)
            else:
                response = RedirectResponse("/", 302)
                session = self._sessions.dumps({"email": email, "iat": int(self.now().timestamp()), "ep": self._epoch()})
                self._cookie(response, SESSION_COOKIE, session, SESSION_MAX_AGE, "/")
            response.delete_cookie(OAUTH_COOKIE, path="/auth", httponly=True, secure=True, samesite="lax")
            return response

        @router.post("/logout")
        def logout(request: Request) -> Response:
            self.check_write(request)
            if self.cfg.auth_mode == "google":
                try:
                    self.owner(request)
                except NotSignedIn:
                    pass  # only the owner may sign out every device; anyone may drop their own cookie
                else:
                    self._bump_epoch()
            response = JSONResponse({"ok": True})
            response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=True, samesite="lax")
            return response

        return router

    def _finish_login(self, cookie: str | None, code: str, state: str) -> str:
        if self.cfg.auth_mode != "google":
            raise LoginRefused("Google sign-in is off in Tailscale mode")
        pending = self._signed(self._oauth, cookie, OAUTH_MAX_AGE)
        if pending is None:
            raise LoginRefused("login cookie missing, forged or expired")
        if not state or not _same(state, str(pending.get("state", ""))):
            raise LoginRefused("state mismatch")
        if not code:
            raise LoginRefused("no authorization code")
        client_secret = secretstore.get(LOGIN_CLIENT_SECRET)
        if not client_secret:
            raise LoginRefused(f"secret '{LOGIN_CLIENT_SECRET}' is not set")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.cfg.login_client_id,
            "client_secret": client_secret,
            "redirect_uri": self.redirect_uri,
            "code_verifier": pending.get("verifier", ""),
        }
        try:
            with httpx.Client(transport=self.token_transport, timeout=10) as client:
                resp = client.post(GOOGLE_TOKEN_URL, data=form)
            token = resp.json().get("id_token") if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as err:
            raise LoginRefused(f"token exchange failed: {type(err).__name__}") from None
        if not isinstance(token, str):
            raise LoginRefused(f"token exchange failed: HTTP {resp.status_code}")
        try:
            claims = self.verify_id_token(token, self.cfg.login_client_id)
        except Exception as err:  # google-auth raises ValueError and several transport errors
            raise LoginRefused(f"ID token rejected: {type(err).__name__}") from None
        if claims.get("aud") != self.cfg.login_client_id:
            raise LoginRefused("ID token audience mismatch")
        if claims.get("email_verified") is not True:
            raise LoginRefused("email not verified")
        email = claims.get("email")
        if not isinstance(email, str) or email.casefold() != self.cfg.owner_email.casefold():
            raise LoginRefused("not the owner's account")
        return email
