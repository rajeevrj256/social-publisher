"""SQLite-backed fixtures so the suite runs with no database server.

Everything under test here is dialect-neutral: the Postgres-only pieces
(advisory locks, SKIP LOCKED) degrade to no-ops and are exercised in staging.
"""

import os
import sys
from datetime import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("APP_ENCRYPTION_KEY",
                      "cE9wRUxJQzhtTFJfUmtpZldQNzJ0X0Y5dEJfWk5rQ0k=")
os.environ.setdefault("VIDEO_ROOT", "/tmp/videos")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")

from src.models import (  # noqa: E402
    Account, AccountSchedule, AccountTheme, Base, MetadataStatus, Platform,
    Theme, Video,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def themes(session):
    created = {}
    for name in ("Motivation", "Success", "Gym", "Supercars"):
        theme = Theme(name=name, active=True)
        session.add(theme)
        created[name] = theme
    session.flush()
    return created


def make_video(session, theme, filename, *, duration=30.0, codec="h264",
               width=1080, height=1920):
    video = Video(filename=filename, filepath=f"{theme.name.lower()}/{filename}",
                  file_hash=f"hash-{filename}", theme_id=theme.id,
                  duration=duration, codec=codec, width=width, height=height,
                  file_size=5_000_000, metadata_status=MetadataStatus.ok)
    session.add(video)
    session.flush()
    return video


def make_account(session, username, platform=Platform.instagram, *,
                 require_approval=False):
    account = Account(platform=platform, username=username,
                      account_name=username, platform_account_id=f"ig-{username}",
                      require_approval=require_approval)
    session.add(account)
    session.flush()
    return account


@pytest.fixture
def factories():
    return {"video": make_video, "account": make_account}
