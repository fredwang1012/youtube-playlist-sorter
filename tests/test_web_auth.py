"""Owner-only sign-in: Google OpenID Connect with PKCE, the Tailscale identity fallback, sessions and the write guard."""
import base64
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import jobs, secretstore
from app import run as runner
from app.config import settings
from app.web import api as web_api
from app.web.server import Deps, create_app
from tests.test_run import NOW, FakeYT, fetcher

PUBLIC = "https://vm.example.ts.net"
CLIENT_ID = "client-123.apps.googleusercontent.com"
OWNER = "owner@example.com"
OWNER_TS = "owner@github"
LOGIN_SECRET = "LOGIN-CLIENT-SECRET-MARKER-7f3a"
WRITE = {"Origin": PUBLIC}  # json= sets the JSON content type

API_ROUTES = [
    ("GET", "/api/me"),
    ("GET", "/api/queue"),
    ("POST", "/api/queue/tick"),
    ("GET", "/api/playlists"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings/queue"),
    ("POST", "/api/run"),
    ("GET", "/api/activity"),
    ("GET", "/api/graph"),
    ("PUT", "/api/graph"),
    ("POST", "/api/graph/preview"),
]


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeGoogle:
    """Google's token endpoint (as an httpx transport) and the ID token verifier."""

    def __init__(self):
        self.claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "email": OWNER, "email_verified": True}
        self.token_status = 200
        self.token_requests: list[tuple[str, dict]] = []
        self.verified: list[tuple[str, str]] = []
        self.verify_error: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.token_requests.append((str(request.url), dict(parse_qsl(request.content.decode()))))
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"id_token": "id-token-1", "access_token": "at", "token_type": "Bearer"})

    def verify(self, token: str, audience: str) -> dict:
        self.verified.append((token, audience))
        if self.verify_error:
            raise self.verify_error
        return dict(self.claims)


@dataclass
class Env:
    client: TestClient
    yt: FakeYT
    google: FakeGoogle
    clock: Clock
    deps: Deps


def make_env(tmp_path, **cfg):
    for key, value in cfg.items():
        setattr(settings, key, value)  # callers restore through monkeypatch
    yt, google, clock = FakeYT(), FakeGoogle(), Clock()
    deps = Deps(
        make_client=lambda: (yt, yt.meter), now=clock, token_transport=httpx.MockTransport(google.handler),
        verify_id_token=google.verify, fetch=fetcher(["v1"]), static_dir=tmp_path / "static",
    )
    client = TestClient(create_app(settings, deps), base_url=PUBLIC, follow_redirects=False)
    return Env(client, yt, google, clock, deps)


@pytest.fixture
def web_settings(monkeypatch, tmp_path):
    for key in ("public_url", "auth_mode", "login_client_id", "graph_path", "owner_email", "owner_tailscale_login"):
        monkeypatch.setattr(settings, key, getattr(settings, key))
    settings.public_url, settings.auth_mode, settings.login_client_id = PUBLIC, "google", CLIENT_ID
    settings.graph_path, settings.owner_email, settings.owner_tailscale_login = tmp_path / "graphs" / "mine.yaml", OWNER, ""
    monkeypatch.setattr(runner, "fetch_url", fetcher(["v1"]))  # a background run must never reach the network
    monkeypatch.setattr(jobs, "_thread", None)
    monkeypatch.setattr(jobs, "_last", None)
    secretstore.set("login-client-secret", LOGIN_SECRET)


@pytest.fixture
def env(web_settings, tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e


def sign_in(client: TestClient) -> httpx.Response:
    r = client.get("/auth/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return client.get("/auth/callback", params={"code": "auth-code-1", "state": state})


@pytest.fixture
def api(env):
    assert sign_in(env.client).status_code == 302
    return env


def call(client, method, path, json=None, headers=WRITE):
    if method == "GET":
        return client.get(path)
    return client.request(method, path, json={} if json is None else json, headers=headers)


# --- every API route needs the owner ---

def test_api_requires_login(env):
    app_api = {(m, r.path) for r in web_api.build_router(settings, env.deps).routes for m in r.methods - {"HEAD"}}
    assert app_api == set(API_ROUTES)  # a new route must be added here, so it gets this check too
    for cookies in ({}, {"ytps_session": "forged.value"}):
        env.client.cookies.clear()
        env.client.cookies.update(cookies)
        for method, path in API_ROUTES:
            r = call(env.client, method, path)
            assert (r.status_code, r.json()) == (401, {"error": "login required"}), (method, path)
    # in Google mode a Tailscale identity header is not a credential
    env.client.cookies.clear()
    r = env.client.get("/api/me", headers={"Tailscale-User-Login": OWNER_TS})
    assert r.status_code == 401
    assert env.client.get("/api/nope").status_code == 401


def test_html_redirects_to_login_and_docs_are_off(env):
    for path in ("/", "/queue", "/docs", "/redoc", "/openapi.json"):
        r = env.client.get(path)
        assert (r.status_code, r.headers["location"]) == (302, "/auth/login"), path


# --- Google sign-in ---

def test_login_redirect_has_pkce_and_minimal_scope(env):
    r = env.client.get("/auth/login")
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["scope"] == "openid email"
    assert q["client_id"] == CLIENT_ID and q["response_type"] == "code"
    assert q["redirect_uri"] == f"{PUBLIC}/auth/callback"
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 32
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("ytps_oauth=") and "HttpOnly" in cookie and "Secure" in cookie and "Path=/auth" in cookie
    assert q["state"] not in cookie  # signed, not readable as plain state
    r = env.client.get("/auth/callback", params={"code": "auth-code-1", "state": q["state"]})
    assert r.status_code == 302 and r.headers["location"] == "/"
    (token_url, form), = env.google.token_requests
    assert token_url == "https://oauth2.googleapis.com/token"
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code-1"
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == LOGIN_SECRET
    assert form["redirect_uri"] == f"{PUBLIC}/auth/callback"
    digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert digest == q["code_challenge"]
    assert env.google.verified == [("id-token-1", CLIENT_ID)]


def test_callback_rejects_bad_state(env):
    env.client.get("/auth/login")
    r = env.client.get("/auth/callback", params={"code": "c", "state": "not-the-state"})
    assert r.status_code == 403 and "text/html" in r.headers["content-type"]
    assert "ytps_session" not in env.client.cookies and env.google.token_requests == []
    env.client.cookies.clear()  # no login cookie at all
    assert env.client.get("/auth/callback", params={"code": "c", "state": "x"}).status_code == 403
    # the login cookie expires after 10 minutes
    r = env.client.get("/auth/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    env.clock.now = NOW + timedelta(minutes=11)
    assert env.client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 403
    assert env.google.token_requests == []
    assert env.client.get("/api/me").status_code == 401


def test_callback_rejects_other_email(env):
    env.google.claims["email"] = "someone.else@example.com"
    r = sign_in(env.client)
    assert r.status_code == 403 and "ytps_session=" not in r.headers.get("set-cookie", "")
    assert env.client.get("/api/me").status_code == 401


def test_callback_rejects_unverified_email(env):
    env.google.claims["email_verified"] = False
    assert sign_in(env.client).status_code == 403
    assert env.client.get("/api/me").status_code == 401


@pytest.mark.parametrize("break_it", ["token_error", "verify_error", "wrong_aud", "no_code"])
def test_callback_rejects_other_failures(env, break_it):
    if break_it == "token_error":
        env.google.token_status = 400
    elif break_it == "verify_error":
        env.google.verify_error = ValueError("Token expired")
    elif break_it == "wrong_aud":
        env.google.claims["aud"] = "someone-else.apps.googleusercontent.com"
    if break_it == "no_code":
        r = env.client.get("/auth/login")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        r = env.client.get("/auth/callback", params={"state": state, "error": "access_denied"})
    else:
        r = sign_in(env.client)
    assert r.status_code == 403
    assert env.client.get("/api/me").status_code == 401


def test_callback_sets_secure_httponly_cookie(env):
    r = sign_in(env.client)
    session = next(c for c in r.headers.get_list("set-cookie") if c.startswith("ytps_session="))
    for attr in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=2592000", "Path=/"):
        assert attr in session, attr
    assert env.client.get("/api/me").json() == {"email": OWNER, "auth_mode": "google"}


def test_session_expiry(env):
    sign_in(env.client)
    env.clock.now = NOW + timedelta(days=29)
    assert env.client.get("/api/me").status_code == 200
    env.clock.now = NOW + timedelta(days=30, seconds=1)
    assert env.client.get("/api/me").status_code == 401


def test_session_for_old_owner_is_refused(env):
    sign_in(env.client)
    settings.owner_email = "someone.new@example.com"
    assert env.client.get("/api/me").status_code == 401


def test_logout_clears_session(api):
    assert api.client.post("/auth/logout", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert api.client.get("/api/me").status_code == 200
    r = api.client.post("/auth/logout", json={}, headers=WRITE)
    assert r.status_code == 200
    assert api.client.get("/api/me").status_code == 401


def test_signed_out_page_is_public_and_links_to_sign_in(env):
    r = env.client.get("/auth/signed-out")
    assert r.status_code == 200 and "Signed out" in r.text and 'href="/auth/login"' in r.text
    assert "content-security-policy" in r.headers


# --- Tailscale fallback ---

def test_tailscale_mode_requires_owner_header(web_settings, tmp_path):
    e = make_env(tmp_path, auth_mode="tailscale", owner_tailscale_login=OWNER_TS)
    with e.client as c:
        assert c.get("/api/me").status_code == 401
        assert c.get("/api/me", headers={"Tailscale-User-Login": ""}).status_code == 401
        r = c.get("/api/me", headers={"Tailscale-User-Login": "someone-else@github"})
        assert (r.status_code, r.json()) == (403, {"error": "not the owner"})
        r = c.get("/api/me", headers={"Tailscale-User-Login": OWNER_TS})
        assert (r.status_code, r.json()) == (200, {"email": OWNER_TS, "auth_mode": "tailscale"})
        # a Google session cookie means nothing here, and HTML gets a page, not a redirect loop
        r = c.get("/")
        assert r.status_code == 401 and "location" not in r.headers
        r = c.get("/auth/login")
        assert (r.status_code, r.headers["location"]) == (302, "/")
        r = c.post("/api/run", json={}, headers={"Tailscale-User-Login": OWNER_TS, "Origin": "https://evil.example"})
        assert r.status_code == 403


def test_tailscale_mode_without_configured_login_refuses_everyone(web_settings, tmp_path):
    e = make_env(tmp_path, auth_mode="tailscale", owner_tailscale_login="")
    with e.client as c:
        assert c.get("/api/me", headers={"Tailscale-User-Login": OWNER_TS}).status_code == 403


# --- write guard ---

def test_post_requires_json_and_origin(api):
    c = api.client
    api.yt.owned.append("PLqueueQueue1")
    body = {"playlist_id": "PLqueueQueue1", "confirm_title": "Title PLqueueQueue1"}
    r = c.post("/api/run", data={"a": "b"}, headers=WRITE)
    assert (r.status_code, r.json()) == (403, {"error": "JSON body required"})
    r = c.post("/api/run", content=b"{}", headers={**WRITE, "Content-Type": "text/plain"})
    assert r.status_code == 403
    r = c.put("/api/settings/queue", json=body, headers={"Origin": "https://evil.example"})
    assert (r.status_code, r.json()) == (403, {"error": "bad origin"})
    assert c.put("/api/settings/queue", json=body).status_code == 403  # no Origin
    assert c.get("/api/settings").json() == {"queue_playlist": None}
    r = c.put("/api/settings/queue", content=b'{"playlist_id": "PLqueueQueue1", "confirm_title": "Title PLqueueQueue1"}',
              headers={**WRITE, "Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200
    assert c.get("/api/settings").json() == {"queue_playlist": "PLqueueQueue1"}


def test_healthz_public_and_minimal(env):
    r = env.client.get("/healthz")
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert "set-cookie" not in r.headers


def test_logout_revokes_copied_cookie(api, tmp_path):
    copied = api.client.cookies.get("ytps_session")
    other = make_env(tmp_path)  # a second device, signed in separately
    with other.client as phone:
        assert sign_in(phone).status_code == 302
        # someone signed out cannot sign the owner out everywhere
        with TestClient(api.client.app, base_url=PUBLIC) as stranger:
            assert stranger.post("/auth/logout", json={}, headers=WRITE).status_code == 200
        assert api.client.get("/api/me").status_code == 200
        assert api.client.post("/auth/logout", json={}, headers=WRITE).status_code == 200
        api.client.cookies.set("ytps_session", copied)
        assert api.client.get("/api/me").status_code == 401
        assert phone.get("/api/me").status_code == 401  # every device is signed out
        api.client.cookies.clear()
        assert sign_in(api.client).status_code == 302  # signing in again works under the new epoch
        assert api.client.get("/api/me").status_code == 200


def test_non_ascii_credentials_are_refused_not_500(web_settings, tmp_path, env):
    env.client.get("/auth/login")
    assert env.client.get("/auth/callback", params={"code": "c", "state": "\u00e9t\u00e9"}).status_code == 403
    e = make_env(tmp_path, auth_mode="tailscale", owner_tailscale_login=OWNER_TS)
    with e.client as c:
        r = c.get("/api/me", headers={"Tailscale-User-Login": "fr\u00f6d@github".encode("utf-8")})
        assert r.status_code == 403


def test_every_non_get_api_route_is_write_guarded(api):
    for method, path in API_ROUTES:
        if method == "GET":
            continue
        r = api.client.request(method, path, json={}, headers={"Origin": "https://evil.example"})
        assert (r.status_code, r.json()) == (403, {"error": "bad origin"}), (method, path)
        r = api.client.request(method, path, content=b"{}", headers={**WRITE, "Content-Type": "text/plain"})
        assert r.status_code == 403, (method, path)


def test_google_mode_needs_owner_email(web_settings, monkeypatch):
    monkeypatch.setattr(settings, "owner_email", "")
    with pytest.raises(ValueError, match="OWNER_EMAIL"):
        create_app(settings, Deps())


@pytest.mark.parametrize("site, status", [(None, 200), ("same-origin", 200), ("same-site", 403), ("cross-site", 403)])
def test_api_refuses_cross_site_fetches(web_settings, tmp_path, site, status):
    e = make_env(tmp_path, auth_mode="tailscale", owner_tailscale_login=OWNER_TS)
    headers = {"Tailscale-User-Login": OWNER_TS, **({"Sec-Fetch-Site": site} if site else {})}
    with e.client as c:
        assert c.get("/api/activity", headers=headers).status_code == status
        assert c.get("/healthz", headers=headers).status_code == 200


def test_google_mode_refuses_cross_site_fetches_too(api):
    assert api.client.get("/api/me", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
