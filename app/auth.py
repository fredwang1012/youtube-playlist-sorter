"""OAuth for a Desktop-app client: loopback consent flow, refresh token kept only in the secret store."""
import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app import secretstore
from app.config import settings

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
REFRESH_TOKEN = "refresh-token"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class AuthError(Exception):
    """A problem the user can fix; the message says how and never contains a token."""


class AuthExpiredError(AuthError):
    """The refresh token was rejected: expired (7 days in Testing mode) or revoked."""


def _read_client_config(path: Path | None = None) -> dict:
    path = path or settings.client_secret_path
    if not path.is_file():
        raise AuthError(
            f"Client secret not found. Create a Desktop-app OAuth client in Google Cloud Console, "
            f"download its JSON and save it as: {path}"
        )
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))["installed"]
        cfg["client_id"], cfg["client_secret"]
    except (ValueError, KeyError, TypeError):
        raise AuthError(f"{path} is not a Desktop-app client secret file (expected an 'installed' section).") from None
    return cfg


def run_auth(client_secret_path: Path | None = None) -> None:
    """Run the loopback consent flow and store the refresh token in the secret store."""
    path = client_secret_path or settings.client_secret_path
    _read_client_config(path)
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, prompt="consent")
    if not creds.refresh_token:
        raise AuthError("Google returned no refresh token. Remove the app's access in your Google account and re-run auth.")
    try:
        secretstore.set(REFRESH_TOKEN, creds.refresh_token)
    except secretstore.SecretStoreError as e:
        raise AuthError(str(e)) from None


def load_credentials() -> Credentials:
    """Credentials with a fresh access token, built from the stored refresh token."""
    cfg = _read_client_config()
    try:
        refresh_token = secretstore.get(REFRESH_TOKEN)
    except secretstore.SecretStoreError as e:
        raise AuthError(str(e)) from None
    if not refresh_token:
        raise AuthError("Not signed in. Run: python -m app.cli auth")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=cfg.get("token_uri", TOKEN_URI),
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except RefreshError:
        raise AuthExpiredError("Google rejected the stored token (revoked or expired). Re-run: python -m app.cli auth") from None
    return creds
