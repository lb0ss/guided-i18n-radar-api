"""Settings: secrets and paths from the environment (.env), what to track from projects/<name>/sources.yaml."""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # already-exported variables win


@dataclass(frozen=True)
class Settings:
    project: str
    sources: dict
    data_dir: Path
    devto_api_key: str | None
    github_token: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    digest_from: str | None
    digest_to: str | None
    pushover_token: str | None
    pushover_user: str | None
    x_bearer_token: str | None = None
    x_monthly_post_limit: int = 600  # about $3 a month at $0.005 a post

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'radar.db'}"


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


@lru_cache
def settings() -> Settings:
    project = os.environ.get("RADAR_PROJECT", "guided-i18n")
    data_dir = Path(os.environ.get("RADAR_DATA_DIR", ROOT / "data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        project=project,
        sources=yaml.safe_load((ROOT / "projects" / project / "sources.yaml").read_text()),
        data_dir=data_dir,
        devto_api_key=_env("DEV_API_KEY"),
        github_token=_env("GITHUB_TOKEN"),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        digest_from=_env("DIGEST_FROM") or _env("SMTP_USER"),
        digest_to=_env("DIGEST_TO"),
        pushover_token=_env("PUSHOVER_TOKEN"),
        pushover_user=_env("PUSHOVER_USER"),
        x_bearer_token=_env("X_BEARER_TOKEN"),
        x_monthly_post_limit=int(os.environ.get("X_MONTHLY_POST_LIMIT", "600")),
    )
