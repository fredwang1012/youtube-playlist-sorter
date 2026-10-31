import pytest

from app import secretstore
from app.config import Settings, settings


@pytest.fixture
def systemd_dir(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    monkeypatch.setattr(settings, "secrets_backend", "systemd")
    monkeypatch.setattr(settings, "credentials_dir", creds)
    return creds


def test_keyring_backend_round_trip(memory_keyring):
    secretstore.set("refresh-token", "x")
    assert secretstore.get("refresh-token") == "x"
    # the owner's existing token lives at exactly this keychain entry
    assert memory_keyring.store[("youtube-playlist-sorter", "refresh-token")] == "x"


def test_keyring_backend_missing_is_none():
    assert secretstore.get("session-key") is None


def test_systemd_backend_reads_file(systemd_dir):
    (systemd_dir / "refresh-token").write_text("abc\n", encoding="utf-8")
    assert secretstore.get("refresh-token") == "abc"
    assert secretstore.get("session-key") is None


def test_systemd_backend_is_read_only(systemd_dir):
    with pytest.raises(secretstore.ReadOnlySecretStore, match="systemd-creds encrypt"):
        secretstore.set("refresh-token", "x")
    assert not (systemd_dir / "refresh-token").exists()


@pytest.mark.parametrize("name", ["../x", "a/b", "a\\b", "..", ""])
def test_systemd_backend_rejects_path_names(systemd_dir, name):
    with pytest.raises(ValueError):
        secretstore.get(name)


def test_systemd_backend_without_credentials_dir_says_so(monkeypatch):
    monkeypatch.setattr(settings, "secrets_backend", "systemd")
    monkeypatch.setattr(settings, "credentials_dir", None)
    with pytest.raises(secretstore.SecretStoreError, match="CREDENTIALS_DIRECTORY"):
        secretstore.get("refresh-token")


def test_credentials_dir_defaults_to_env_for_systemd(monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert Settings(secrets_backend="systemd").credentials_dir == tmp_path
    assert Settings(secrets_backend="keyring").credentials_dir is None


def test_server_setting_defaults(monkeypatch):
    for var in ("SECRETS_BACKEND", "CREDENTIALS_DIRECTORY", "AUTH_MODE", "OWNER_EMAIL", "PUBLIC_URL",
                "BIND_PORT", "TICK_QUOTA_CEILING", "GRAPH_PATH", "RUN_INTERVAL_HOURS"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert s.secrets_backend == "keyring"
    assert s.credentials_dir is None
    assert s.auth_mode == "google"
    assert s.owner_email == "owner@example.com"
    assert s.owner_tailscale_login == ""
    assert s.public_url == "http://127.0.0.1:8765"
    assert s.bind_port == 8765
    assert s.tick_quota_ceiling == 9500
    assert s.login_client_id == ""
    assert s.graph_path == Settings.model_fields["graph_path"].default
    assert s.graph_path.parts[-2:] == ("graphs", "mine.yaml")
    assert s.run_interval_hours == 6


def test_relative_graph_path_anchored_to_root():
    from app.config import ROOT
    assert Settings(graph_path="graphs/x.yaml").graph_path == ROOT / "graphs" / "x.yaml"
