"""Relational model.

The organising principle is that publication state is per (video, account,
platform) - never a boolean on the video. A video is only "used up" relative to
one account on one platform, so the same file can legitimately serve twenty
accounts while retaining full, separate history for each.
"""

from __future__ import annotations

import enum
from datetime import datetime, time

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, DateTime, Enum, Float,
    ForeignKey, Index, Integer, String, Text, Time, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres, plain JSON on SQLite so the test suite can run without a
# database server.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Platform(str, enum.Enum):
    instagram = "instagram"
    youtube = "youtube"


class PublicationStatus(str, enum.Enum):
    pending = "pending"          # row created, not yet queued
    awaiting_metadata = "awaiting_metadata"   # needs a title/caption from you
    awaiting_approval = "awaiting_approval"  # shown on Telegram, waiting on a human
    approved = "approved"        # human said yes; safe to queue
    rejected = "rejected"        # human said no; never publishes
    queued = "queued"            # handed to the worker queue
    publishing = "publishing"    # worker owns it; crash recovery looks here
    published = "published"
    failed = "failed"            # retryable
    failed_permanent = "failed_permanent"
    needs_review = "needs_review"  # may exist on the platform; do not retry blind
    cancelled = "cancelled"


class MetadataStatus(str, enum.Enum):
    pending = "pending"
    ok = "ok"
    invalid = "invalid"
    missing = "missing"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False)


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[Platform] = mapped_column(
        Enum(Platform, name="platform_enum"), nullable=False)
    account_name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str] = mapped_column(String(120), nullable=False)
    # IG user id, or YouTube channel id.
    platform_account_id: Mapped[str | None] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata",
                                          nullable=False)
    # Nothing leaves the machine until a human approves it on Telegram. Opt-out
    # is per account so a low-stakes channel can run unattended.
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True,
                                                   nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    credentials: Mapped[AccountCredential | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan")
    themes: Mapped[list[AccountTheme]] = relationship(
        back_populates="account", cascade="all, delete-orphan")
    schedules: Mapped[list[AccountSchedule]] = relationship(
        back_populates="account", cascade="all, delete-orphan")
    ai_config: Mapped[AccountAIConfig | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("platform", "username", name="uq_account_platform_username"),
        Index("ix_accounts_enabled", "enabled"),
    )

    def __repr__(self) -> str:
        return f"<Account {self.platform.value}:@{self.username}>"


class AccountCredential(Base, TimestampMixin):
    """OAuth material only. No passwords are ever accepted or stored."""

    __tablename__ = "account_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True)
    # OAuth *app* credentials, per account. Different Google accounts mean
    # different Cloud projects, so one global client id cannot serve them all -
    # and separate projects each get their own upload quota.
    client_id: Mapped[str | None] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str | None] = mapped_column(Text)

    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict | None] = mapped_column(JSONType)

    account: Mapped[Account] = relationship(back_populates="credentials")


class Theme(Base):
    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Every account on this theme publishes the SAME video each cycle, chosen
    # once and fanned out. On by default: with one account it changes nothing,
    # and with several it is almost always what sharing a theme is meant to do.
    # Approvals stay per account either way. Set false for independent queues.
    coordinated: Mapped[bool] = mapped_column(Boolean, default=True,
                                              nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<Theme {self.name}>"


class AccountTheme(Base):
    """What an account is allowed to publish, and how often relative to peers."""

    __tablename__ = "account_themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    posting_frequency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Relative weight used for theme selection: 10 vs 5 means roughly 2:1.
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    account: Mapped[Account] = relationship(back_populates="themes")
    theme: Mapped[Theme] = relationship()

    __table_args__ = (
        UniqueConstraint("account_id", "theme_id", name="uq_account_theme"),
        Index("ix_account_themes_account", "account_id"),
        CheckConstraint("priority >= 0", name="ck_account_theme_priority"),
    )


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Relative to VIDEO_ROOT, so the library survives being moved or remounted.
    filepath: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Identifier at the source for videos that are not stored locally, e.g. a
    # Google Drive file id. The bytes are fetched only when publishing, so a
    # 20 GB library costs nothing on disk.
    remote_id: Mapped[str | None] = mapped_column(String(255))
    # The human-readable link, kept alongside the id purely so the row is
    # legible when browsing the table.
    remote_url: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    codec: Mapped[str | None] = mapped_column(String(32))
    theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id", ondelete="SET NULL"))
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"))
    metadata_status: Mapped[MetadataStatus] = mapped_column(
        Enum(MetadataStatus, name="metadata_status_enum"),
        default=MetadataStatus.pending, nullable=False)
    metadata_error: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    theme: Mapped[Theme | None] = relationship()
    source: Mapped["Source | None"] = relationship()

    __table_args__ = (
        Index("ix_videos_file_hash", "file_hash"),
        Index("ix_videos_theme", "theme_id"),
        # The scheduler's hot path: eligible videos for a theme.
        Index("ix_videos_theme_status_active", "theme_id", "metadata_status", "active"),
        Index("ix_videos_source", "source_id"),
        # The scheduler's hot path once sources are in play.
        Index("ix_videos_source_status", "source_id", "metadata_status", "active"),
        Index("ix_videos_remote_id", "remote_id"),
    )

    def __repr__(self) -> str:
        return f"<Video {self.filename}>"


class VideoAccountMapping(Base, TimestampMixin):
    """Explicit eligibility of a video for an account.

    When a video has no mapping rows at all it falls back to theme eligibility;
    once any row exists the mapping is authoritative, which is what makes
    "this clip is for the big account only" expressible.
    """

    __tablename__ = "video_account_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="eligible", nullable=False)

    video: Mapped[Video] = relationship()
    account: Mapped[Account] = relationship()

    __table_args__ = (
        UniqueConstraint("video_id", "account_id", name="uq_video_account"),
        Index("ix_vam_account", "account_id"),
        Index("ix_vam_video", "video_id"),
        Index("ix_vam_account_enabled", "account_id", "enabled"),
    )


class Publication(Base, TimestampMixin):
    """One row per (video, account, platform) attempt - the history of record.

    The unique constraint is the duplicate rule: the same video cannot be
    intentionally published twice to the same account on the same platform,
    while the same video across different accounts is perfectly valid.
    Retries update this row rather than inserting another.
    """

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    platform: Mapped[Platform] = mapped_column(
        Enum(Platform, name="platform_enum"), nullable=False)

    platform_post_id: Mapped[str | None] = mapped_column(String(255))
    platform_url: Mapped[str | None] = mapped_column(Text)

    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[str | None] = mapped_column(Text)

    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus, name="publication_status_enum"),
        default=PublicationStatus.pending, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Survives a crash mid-upload so a resumed job can recognise its own work.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True))
    # Which proposal this is for its slot. A rejection offers a different video
    # and increments this, so the retry budget is per slot, not per video.
    proposal_attempt: Mapped[int] = mapped_column(Integer, default=1,
                                                  nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    # Shared by every publication of one coordinated fan-out, so a single
    # approval can release the whole set.
    group_key: Mapped[str | None] = mapped_column(String(64))

    video: Mapped[Video] = relationship()
    account: Mapped[Account] = relationship()

    __table_args__ = (
        UniqueConstraint("video_id", "account_id", "platform",
                         name="uq_publication_video_account_platform"),
        Index("ix_pub_account", "account_id"),
        Index("ix_pub_video", "video_id"),
        Index("ix_pub_status", "status"),
        Index("ix_pub_scheduled_at", "scheduled_at"),
        Index("ix_pub_account_status", "account_id", "status"),
        Index("ix_pub_published_at", "published_at"),
        Index("ix_pub_group_key", "group_key"),
    )

    def __repr__(self) -> str:
        return f"<Publication v{self.video_id} a{self.account_id} {self.status.value}>"


class AccountSchedule(Base):
    """When an account publishes. day_of_week uses 0=Monday .. 6=Sunday,
    NULL meaning every day."""

    __tablename__ = "account_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    day_of_week: Mapped[int | None] = mapped_column(Integer)
    publish_time: Mapped[time] = mapped_column(Time, nullable=False)
    videos_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    account: Mapped[Account] = relationship(back_populates="schedules")

    __table_args__ = (
        Index("ix_schedules_account", "account_id", "enabled"),
        CheckConstraint("day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)",
                        name="ck_schedule_dow"),
        CheckConstraint("videos_per_day >= 1", name="ck_schedule_per_day"),
    )


class Schedule(Base, TimestampMixin):
    """A publishing slot shared by several accounts.

    One schedule fires once, picks one video, and gives it to every account it
    owns. Per-account schedules cannot express this: two accounts with their own
    rows fire independently, and with a coordinated theme each fan-out reaches
    the other, so both accounts end up publishing twice.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    publish_time: Mapped[time] = mapped_column(Time, nullable=False)
    day_of_week: Mapped[int | None] = mapped_column(Integer)
    videos_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata",
                                          nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional: restrict this slot to one theme. Otherwise the members' own
    # theme mappings decide.
    theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id", ondelete="SET NULL"))

    accounts: Mapped[list[ScheduleAccount]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_schedules_enabled", "enabled"),
        CheckConstraint("day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)",
                        name="ck_schedules_dow"),
        CheckConstraint("videos_per_day >= 1", name="ck_schedules_per_day"),
    )

    def __repr__(self) -> str:
        return f"<Schedule {self.name} @{self.publish_time}>"


class ScheduleAccount(Base):
    """Which accounts a shared schedule publishes for."""

    __tablename__ = "schedule_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    schedule: Mapped[Schedule] = relationship(back_populates="accounts")
    account: Mapped[Account] = relationship()

    __table_args__ = (
        UniqueConstraint("schedule_id", "account_id", name="uq_schedule_account"),
        Index("ix_schedule_accounts_account", "account_id"),
    )


class AccountAIConfig(Base, TimestampMixin):
    """Per-account voice. Two accounts posting the same clip should not read as
    the same author, so the prompt lives with the account."""

    __tablename__ = "account_ai_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, unique=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    title_template: Mapped[str | None] = mapped_column(Text)
    caption_template: Mapped[str | None] = mapped_column(Text)
    hashtag_rules: Mapped[str | None] = mapped_column(Text)

    account: Mapped[Account] = relationship(back_populates="ai_config")


class HashtagSet(Base):
    """Default hashtags for an account, optionally narrowed to one theme.
    theme_id NULL is the account-wide default."""

    __tablename__ = "hashtag_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"))
    hashtags: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("account_id", "theme_id", name="uq_hashtag_account_theme"),
        Index("ix_hashtags_account", "account_id"),
    )


class SourceKind(str, enum.Enum):
    local = "local"            # a folder under VIDEO_ROOT
    remote_url = "remote_url"  # a public base URL the platform can fetch from
    gdrive = "gdrive"          # a shared Google Drive folder, fetched per video


class Source(Base, TimestampMixin):
    """Where an account's videos come from.

    Accounts do not share one library by default: a source is a distinct link or
    folder, and an account draws only from the sources mapped to it. Two accounts
    may share a source, which is what lets one link feed both a YouTube channel
    and an Instagram account.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[SourceKind] = mapped_column(
        Enum(SourceKind, name="source_kind_enum"),
        default=SourceKind.local, nullable=False)
    # local: path relative to VIDEO_ROOT. remote_url: https base URL.
    location: Mapped[str] = mapped_column(String(1024), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Every video indexed from this source gets this theme. Without it a
    # re-index cannot tell which theme a remote file belongs to.
    videos_theme_id: Mapped[int | None] = mapped_column(
        ForeignKey("themes.id", ondelete="SET NULL"))

    def __repr__(self) -> str:
        return f"<Source {self.name} ({self.kind.value}:{self.location})>"


class AccountSource(Base):
    """Which sources an account may draw from. Many-to-many on purpose."""

    __tablename__ = "account_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    account: Mapped[Account] = relationship()
    source: Mapped[Source] = relationship()

    __table_args__ = (
        UniqueConstraint("account_id", "source_id", name="uq_account_source"),
        Index("ix_account_sources_account", "account_id", "enabled"),
    )


class FolderThemeMap(Base):
    """Folder name under VIDEO_ROOT -> theme. Keeps ingestion declarative."""

    __tablename__ = "folder_theme_map"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)

    theme: Mapped[Theme] = relationship()
