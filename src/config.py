"""Runtime configuration, sourced entirely from the environment.

Nothing secret is ever hard-coded or defaulted to a working value: a missing
credential must fail loudly at startup rather than silently publish nowhere.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "social_publisher"
    postgres_user: str = "publisher"
    postgres_password: str = ""
    database_url: str | None = None

    redis_url: str = "redis://redis:6379/0"

    app_encryption_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    video_root: str = "/videos/to_publish"
    public_media_base_url: str = ""

    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_graph_version: str = "v25.0"
    # graph.instagram.com = Instagram Login (no Facebook Page needed).
    # graph.facebook.com  = Facebook Login (Page-linked accounts).
    # Override per account via account_credentials.extra["graph_host"].
    instagram_graph_host: str = "graph.instagram.com"
    instagram_daily_limit: int = 100

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    # videos.insert is capped at 100 calls/day in the Video Uploads quota bucket.
    youtube_daily_limit: int = 100

    # Admin mapping API. Bound to localhost by default: it can change what
    # gets published, so it is not exposed on the network without intent.
    admin_api_token: str = ""
    admin_api_host: str = "127.0.0.1"
    admin_api_port: int = 8088

    anthropic_api_key: str = ""
    ai_model: str = "claude-sonnet-5"

    # Drive folder listing. gdown scrapes the share page and refuses folders
    # over 50 files; the official Drive API pages through any size. A key is
    # read-only and safe for link-shared folders, and is created in the same
    # Google Cloud project as the YouTube client.
    google_api_key: str | None = None

    # Rows committed per batch while indexing. One commit at the end meant a
    # crash at file 2700 of 2704 lost everything and the visible count stayed
    # at 0 for the whole run; one commit per file would pay a network
    # round-trip 2704 times over. Scanning is idempotent -- identity is the
    # Drive file id -- so a partial batch is safe to keep and a re-run skips it.
    scan_commit_batch: int = 200

    max_retries: int = 3
    # After this many rejected proposals the slot is abandoned for the day
    # rather than continuing to pester with more videos.
    rejection_limit: int = 5
    # Hour (account-local) by which an approval request must be answered. An
    # unanswered one is deferred rather than left to rot: the slot is gone, but
    # the video returns to the pool the next day instead of being burned.
    approval_cutoff_hour: int = 23
    scheduler_tick_seconds: int = 60
    stuck_job_minutes: int = 30
    log_level: str = "INFO"
    tz: str = Field(default="Asia/Kolkata")

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
