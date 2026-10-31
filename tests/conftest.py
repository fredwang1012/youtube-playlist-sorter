import json

import keyring
import pytest
from keyring.backend import KeyringBackend

from app.config import settings


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keyring():
    """Never touch the real OS keychain."""
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "client_secret_path", tmp_path / "client_secret.json")
    monkeypatch.setattr(settings, "secrets_backend", "keyring")
    monkeypatch.setattr(settings, "credentials_dir", None)


@pytest.fixture
def client_secret(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(
        json.dumps({"installed": {"client_id": "id.apps.example", "client_secret": "shh",
                                  "token_uri": "https://oauth2.googleapis.com/token"}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _no_rss_retry_delay(monkeypatch):
    from app import rss
    monkeypatch.setattr(rss, "RETRY_DELAY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _empty_queue_cache():
    """The queue listing cache is per process; tests must not see each other's entries."""
    from app import queue
    queue.invalidate()
    yield
    queue.invalidate()
