"""Settings read from the environment or the repo-root .env file; a relative DATA_DIR is resolved against the repo root."""
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]  # youtube-playlist-sorter/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    # lives outside the repo so the secret can never be committed
    client_secret_path: Path = Path("~/.config/youtube-playlist-sorter/client_secret.json")
    daily_quota_budget: int = 8000
    allow_any_playlist: bool = False

    @field_validator("data_dir")
    @classmethod
    def _anchor_to_root(cls, v: Path) -> Path:
        # the CLI may be started from any directory, so a relative path must not depend on the working directory
        return v if v.is_absolute() else ROOT / v

    @field_validator("client_secret_path")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return v.expanduser()


settings = Settings()
