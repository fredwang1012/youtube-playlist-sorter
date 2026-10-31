"""Settings read from the environment or the repo-root .env file; relative DATA_DIR and GRAPH_PATH resolve against the repo root."""
import os
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]  # youtube-playlist-sorter/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    # lives outside the repo so the secret can never be committed
    client_secret_path: Path = Path("~/.config/youtube-playlist-sorter/client_secret.json")
    daily_quota_budget: int = 8000
    allow_any_playlist: bool = False

    # server
    secrets_backend: Literal["keyring", "systemd"] = "keyring"
    credentials_dir: Path | None = None
    auth_mode: Literal["google", "tailscale"] = "google"
    owner_email: str = ""  # required in Google mode; the server refuses to start without it
    owner_tailscale_login: str = ""
    public_url: str = "http://127.0.0.1:8765"
    bind_port: int = 8765
    tick_quota_ceiling: int = 9500
    login_client_id: str = ""
    graph_path: Path = ROOT / "graphs" / "mine.yaml"
    # set on the laptop once the VM does the real runs: separate state.db files would file videos twice
    real_runs_disabled: bool = False

    @field_validator("data_dir", "graph_path")
    @classmethod
    def _anchor_to_root(cls, v: Path) -> Path:
        # the CLI may be started from any directory, so a relative path must not depend on the working directory
        return v if v.is_absolute() else ROOT / v

    @field_validator("client_secret_path")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return v.expanduser()

    @model_validator(mode="after")
    def _systemd_credentials_dir(self) -> "Settings":
        # systemd sets CREDENTIALS_DIRECTORY for the service; the field only needs setting to override it
        if self.secrets_backend == "systemd" and self.credentials_dir is None and os.environ.get("CREDENTIALS_DIRECTORY"):
            self.credentials_dir = Path(os.environ["CREDENTIALS_DIRECTORY"])
        return self


settings = Settings()
