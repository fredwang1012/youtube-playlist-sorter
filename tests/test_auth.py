import json
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from httplib2 import Response

from app import auth, cli, secretstore
from app.config import ROOT, Settings
from app.youtube import ForbiddenError, NotFoundError, QuotaExceededError, QuotaMeter, RateLimitedError, RealYouTube


class FakeRequest:
    def __init__(self, resp=None, error=None):
        self.resp, self.error = resp, error

    def execute(self):
        if self.error:
            raise self.error
        return self.resp


class FakeResource:
    """Stands in for one googleapiclient resource; records the kwargs of each call."""

    def __init__(self, responses, log, name):
        self.responses, self.log, self.name = list(responses), log, name

    def _call(self, method, **kw):
        self.log.append((self.name, method, kw))
        nxt = self.responses.pop(0)
        return FakeRequest(error=nxt) if isinstance(nxt, Exception) else FakeRequest(resp=nxt)

    def list(self, **kw):
        return self._call("list", **kw)

    def insert(self, **kw):
        return self._call("insert", **kw)


class FakeService:
    def __init__(self, **responses):
        self.log = []
        self._res = {k: FakeResource(v, self.log, k) for k, v in responses.items()}

    def __getattr__(self, name):
        return lambda: self._res[name]


def http_error(status, reason):
    body = json.dumps({"error": {"errors": [{"reason": reason, "message": "SECRET BODY"}]}}).encode()
    return HttpError(Response({"status": status}), body)


# --- settings ---

def test_relative_data_dir_anchored_to_root():
    assert Settings(data_dir="data").data_dir == ROOT / "data"


def test_defaults():
    s = Settings()
    assert s.daily_quota_budget == 8000
    assert s.client_secret_path.parts[-2:] == ("youtube-playlist-sorter", "client_secret.json")
    assert "~" not in str(s.client_secret_path)


# --- auth ---

def test_missing_client_secret_message_says_where(tmp_path):
    with pytest.raises(auth.AuthError) as e:
        auth.load_credentials()
    assert str(tmp_path / "client_secret.json") in str(e.value)


def test_not_signed_in(client_secret):
    with pytest.raises(auth.AuthError, match="python -m app.cli auth"):
        auth.load_credentials()


def test_run_auth_stores_token_in_keychain(client_secret, memory_keyring, monkeypatch, capsys):
    seen = {}

    class FakeFlow:
        def run_local_server(self, **kw):
            seen.update(kw)
            return SimpleNamespace(refresh_token="refresh-abc")

    def fake_from_file(path, scopes):
        seen["scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", fake_from_file)
    auth.run_auth()
    assert memory_keyring.store[("youtube-playlist-sorter", "refresh-token")] == "refresh-abc"
    assert secretstore.get("refresh-token") == "refresh-abc"
    assert seen["scopes"] == ["https://www.googleapis.com/auth/youtube.force-ssl"]
    assert seen["host"] == "127.0.0.1"
    assert seen["prompt"] == "consent"
    out = capsys.readouterr()
    assert "refresh-abc" not in out.out + out.err


def test_load_credentials_refreshes_from_keychain(client_secret, memory_keyring, monkeypatch):
    memory_keyring.set_password("youtube-playlist-sorter", "refresh-token", "refresh-abc")
    monkeypatch.setattr(auth.Credentials, "refresh", lambda self, request: setattr(self, "token", "access-1"))
    creds = auth.load_credentials()
    assert creds.token == "access-1"
    assert creds.refresh_token == "refresh-abc"
    assert creds.client_id == "id.apps.example"


def test_revoked_token_says_rerun_auth(client_secret, memory_keyring, monkeypatch):
    memory_keyring.set_password("youtube-playlist-sorter", "refresh-token", "refresh-abc")

    def boom(self, request):
        raise RefreshError("invalid_grant refresh-abc")

    monkeypatch.setattr(auth.Credentials, "refresh", boom)
    with pytest.raises(auth.AuthError) as e:
        auth.load_credentials()
    assert "re-run" in str(e.value).lower()
    assert "refresh-abc" not in str(e.value)


def test_load_credentials_from_systemd_credentials(client_secret, tmp_path, monkeypatch):
    creds_dir = tmp_path / "creds"
    creds_dir.mkdir()
    (creds_dir / "refresh-token").write_text("refresh-vm\n", encoding="utf-8")
    monkeypatch.setattr(auth.settings, "secrets_backend", "systemd")
    monkeypatch.setattr(auth.settings, "credentials_dir", creds_dir)
    monkeypatch.setattr(auth.Credentials, "refresh", lambda self, request: setattr(self, "token", "access-1"))
    assert auth.load_credentials().refresh_token == "refresh-vm"


def test_run_auth_on_read_only_store_explains(client_secret, tmp_path, monkeypatch):
    monkeypatch.setattr(auth.settings, "secrets_backend", "systemd")
    monkeypatch.setattr(auth.settings, "credentials_dir", tmp_path)

    class FakeFlow:
        def run_local_server(self, **kw):
            return SimpleNamespace(refresh_token="refresh-abc")

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", lambda path, scopes: FakeFlow())
    with pytest.raises(auth.AuthError) as e:
        auth.run_auth()
    assert "systemd-creds encrypt" in str(e.value)
    assert "refresh-abc" not in str(e.value)


def test_cli_reports_auth_error_without_traceback(capsys):
    assert cli.main(["whoami"]) == 1
    assert "Client secret not found" in capsys.readouterr().err


# --- youtube wrapper ---

def test_videos_batches_at_50_and_meters_each_call():
    ids = [f"v{i}" for i in range(120)]
    svc = FakeService(videos=[{"items": [{"id": "a"}]}, {"items": [{"id": "b"}]}, {"items": [{"id": "c"}]}])
    meter = QuotaMeter()
    out = RealYouTube(svc, meter).videos(ids)
    assert [len(c[2]["id"].split(",")) for c in svc.log] == [50, 50, 20]
    assert [v["id"] for v in out] == ["a", "b", "c"]
    assert meter.total == 3


def test_videos_empty_costs_nothing():
    meter = QuotaMeter()
    assert RealYouTube(FakeService(), meter).videos([]) == []
    assert meter.total == 0


def test_insert_costs_50_and_read_costs_1():
    svc = FakeService(
        playlistItems=[{}],
        channels=[{"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]}],
    )
    meter = QuotaMeter()
    yt = RealYouTube(svc, meter)
    assert yt.channels_uploads_playlist("UC1") == "UU1"
    yt.insert_into_playlist("PL1", "vid")
    assert meter.by_call == {"channels.list": 1, "playlistItems.insert": 50}
    assert meter.total == 51
    assert svc.log[1][2]["body"]["snippet"]["resourceId"]["videoId"] == "vid"


def test_pagination_costs_one_unit_per_page():
    svc = FakeService(subscriptions=[{"items": [{"id": 1}], "nextPageToken": "t"}, {"items": [{"id": 2}]}])
    meter = QuotaMeter()
    assert len(RealYouTube(svc, meter).subscriptions()) == 2
    assert meter.total == 2


def test_playlist_items_respects_max():
    page = {"items": [{"id": i} for i in range(5)], "nextPageToken": "t"}
    svc = FakeService(playlistItems=[page, page])
    meter = QuotaMeter()
    assert len(RealYouTube(svc, meter).playlist_items("UU1", 3)) == 3
    assert meter.total == 1


@pytest.mark.parametrize("status,reason,exc", [
    (403, "quotaExceeded", QuotaExceededError),
    (403, "dailyLimitExceeded", QuotaExceededError),
    (403, "rateLimitExceeded", RateLimitedError),
    (403, "userRateLimitExceeded", RateLimitedError),
    (403, "forbidden", ForbiddenError),
    (404, "playlistNotFound", NotFoundError),
])
def test_http_errors_mapped_without_body(status, reason, exc):
    svc = FakeService(playlists=[http_error(status, reason)])
    meter = QuotaMeter()
    with pytest.raises(exc) as e:
        RealYouTube(svc, meter).my_playlists()
    assert "SECRET BODY" not in str(e.value)
    assert meter.total == 1  # failed requests are still charged


def test_rate_limit_message_is_not_the_quota_message():
    svc = FakeService(playlists=[http_error(403, "rateLimitExceeded"), http_error(403, "quotaExceeded")])
    yt = RealYouTube(svc, QuotaMeter())
    with pytest.raises(RateLimitedError) as rate:
        yt.my_playlists()
    with pytest.raises(QuotaExceededError) as quota:
        yt.my_playlists()
    assert "midnight" not in str(rate.value)
    assert "midnight Pacific" in str(quota.value)
    assert not isinstance(rate.value, QuotaExceededError)


def test_playlist_items_zero_max_makes_no_call():
    svc = FakeService()
    meter = QuotaMeter()
    assert RealYouTube(svc, meter).playlist_items("UU1", 0) == []
    assert svc.log == [] and meter.total == 0


def test_cli_playlists_lists_ids(monkeypatch, capsys):
    svc = FakeService(playlists=[{"items": [{"id": "PL1", "snippet": {"title": "Music"}}]}])
    monkeypatch.setattr(cli, "_client", lambda: (RealYouTube(svc, QuotaMeter()), None))
    assert cli.main(["playlists"]) == 0
    assert "PL1  Music" in capsys.readouterr().out


def test_cli_survives_emoji_on_cp1252_console(monkeypatch):
    import io
    import sys
    from app import cli
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    cli._safe_console()
    print("PL123  hype \U0001f525 mix")
    out.flush()
    assert "hype 🔥 mix".encode("utf-8") in out.buffer.getvalue()
