"""Named secrets from the OS keychain (local default) or systemd credentials (read-only, on the server)."""
import re

import keyring

from app.config import settings

KEYRING_SERVICE = "youtube-playlist-sorter"
# credential names become file names under CREDENTIALS_DIRECTORY, so nothing that could leave it
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


class SecretStoreError(Exception):
    """The secret store is misconfigured; the message says how to fix it and never contains a secret."""


class ReadOnlySecretStore(SecretStoreError):
    """The systemd backend cannot be written from inside the service."""


def _check_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(f"Invalid secret name: {name!r}")


def get(name: str) -> str | None:
    _check_name(name)
    if settings.secrets_backend == "keyring":
        return keyring.get_password(KEYRING_SERVICE, name)
    if settings.credentials_dir is None:
        raise SecretStoreError(
            "SECRETS_BACKEND=systemd but CREDENTIALS_DIRECTORY is not set. "
            "Load the secrets with LoadCredentialEncrypted= in the service unit."
        )
    path = settings.credentials_dir / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def set(name: str, value: str) -> None:  # noqa: A001 - module-level API reads as secretstore.set
    _check_name(name)
    if settings.secrets_backend == "keyring":
        keyring.set_password(KEYRING_SERVICE, name, value)
        return
    raise ReadOnlySecretStore(
        f"Secret '{name}' cannot be saved on the server. Encrypt it on the VM with "
        f"'systemd-creds encrypt --name={name} - /etc/credstore.encrypted/{name}' and restart the service."
    )
