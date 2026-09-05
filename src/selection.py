"""Choosing what an account posts next, and claiming it safely.

Selection is deliberately per-account. There is no global queue: two accounts
sharing a theme must be able to draw the same video independently, and one
account running dry must not stall the others.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from .ffmpeg_utils import VideoInfo, check_compatibility
from .models import (
    Account, AccountSource, AccountTheme, MetadataStatus, Publication,
    PublicationStatus, Source, Theme, Video, VideoAccountMapping,
)

log = logging.getLogger(__name__)


def eligible_themes(session: Session, account: Account) -> list[AccountTheme]:
    return list(session.scalars(
        select(AccountTheme)
        .join(Theme, Theme.id == AccountTheme.theme_id)
        .where(AccountTheme.account_id == account.id,
               AccountTheme.enabled.is_(True),
               Theme.active.is_(True))
        .order_by(AccountTheme.priority.desc())
    ))


def pick_theme(themes: list[AccountTheme], rng: random.Random | None = None
               ) -> AccountTheme | None:
    """Weighted choice by priority, so 10 vs 5 posts roughly twice as often
    without ever starving the lighter theme the way strict ordering would."""
    if not themes:
        return None
    weights = [max(t.priority, 0) for t in themes]
    if sum(weights) <= 0:
        weights = [1] * len(themes)
    return (rng or random).choices(themes, weights=weights, k=1)[0]


def candidate_video_query(account: Account, theme_id: int | None):
    """Videos this account may still post.

    Two exclusions matter: anything already carrying a publication row for this
    account and platform (the duplicate rule), and anything whose explicit
    mapping excludes this account. A video with no mapping rows at all is
    governed by theme alone.
    """
    # Any existing row excludes the video for this account+platform, whatever
    # its status -- with one exception. The unique constraint on
    # (video, account, platform) permits exactly one row, so treating a failed
    # or rejected one as "available again" only produces an IntegrityError at
    # insert time. Re-attempting a failure means updating that row (/retry),
    # not creating a second one.
    #
    # The exception is an expired `deferred` row: "not today" is a decision
    # about this slot, not about the video. Once available_after passes, the
    # row stops blocking and reserve_publication reuses it in place.
    already = (
        select(Publication.id)
        .where(Publication.video_id == Video.id,
               Publication.account_id == account.id,
               Publication.platform == account.platform,
               ~and_(Publication.status == PublicationStatus.deferred,
                     Publication.available_after.isnot(None),
                     Publication.available_after <= func.now()))
        .correlate(Video)
    )

    has_any_mapping = (
        select(VideoAccountMapping.id)
        .where(VideoAccountMapping.video_id == Video.id)
        .correlate(Video)
    )
    mapped_to_account = (
        select(VideoAccountMapping.id)
        .where(VideoAccountMapping.video_id == Video.id,
               VideoAccountMapping.account_id == account.id,
               VideoAccountMapping.enabled.is_(True))
        .correlate(Video)
    )

    query = (
        select(Video)
        .where(
            Video.active.is_(True),
            # `pending` covers videos indexed from a remote source, whose
            # duration and codec are unknown until the file is fetched. They are
            # validated after the just-in-time download instead.
            Video.metadata_status.in_([MetadataStatus.ok,
                                       MetadataStatus.pending]),
            ~exists(already),
            or_(~exists(has_any_mapping), exists(mapped_to_account)),
        )
    )
    if theme_id is not None:
        query = query.where(Video.theme_id == theme_id)

    # Each account draws only from its own sources. An account with no sources
    # mapped falls back to the whole library, so existing single-library setups
    # keep working unchanged.
    source_ids = account_source_ids(account)
    if source_ids is not None:
        query = query.where(Video.source_id.in_(source_ids))
    return query


def account_source_ids(account: Account) -> list[int] | None:
    """Source ids this account may draw from, or None when unrestricted."""
    from sqlalchemy.orm import object_session

    session = object_session(account)
    if session is None:
        return None
    rows = list(session.scalars(
        select(AccountSource.source_id)
        .join(Source, Source.id == AccountSource.source_id)
        .where(AccountSource.account_id == account.id,
               AccountSource.enabled.is_(True),
               Source.enabled.is_(True))
    ))
    return rows or None


def select_next_video(session: Session, account: Account, *,
                      theme_id: int | None = None,
                      rng: random.Random | None = None) -> tuple[Video | None, str | None]:
    """Return a locked, compatible video plus the reason none was found.

    FOR UPDATE SKIP LOCKED is what lets several workers reserve concurrently:
    each skips rows another transaction already holds instead of blocking.
    """
    themes = eligible_themes(session, account)
    if theme_id is None:
        if not themes:
            return None, "no themes mapped to this account"
        chosen = pick_theme(themes, rng)
        theme_ids = [chosen.theme_id] if chosen else []
        # Fall back through the remaining themes when the first runs dry.
        theme_ids += [t.theme_id for t in themes if t.theme_id not in theme_ids]
    else:
        theme_ids = [theme_id]

    is_postgres = session.bind.dialect.name == "postgresql"
    rejected: list[str] = []

    for candidate_theme in theme_ids:
        query = (
            candidate_video_query(account, candidate_theme)
            .order_by(Video.id.asc())
            .limit(25)
        )
        if is_postgres:
            query = query.with_for_update(skip_locked=True, of=Video)

        for video in session.scalars(query):
            if video.metadata_status == MetadataStatus.pending:
                # Nothing to check yet; the publish step validates it once the
                # bytes are in hand.
                return video, None
            info = VideoInfo(duration=video.duration, width=video.width,
                             height=video.height, fps=video.fps,
                             codec=video.codec, file_size=video.file_size)
            ok, reason = check_compatibility(info, account.platform.value)
            if ok:
                return video, None
            rejected.append(f"{video.filename}: {reason}")

    if rejected:
        return None, f"no compatible video ({len(rejected)} rejected, e.g. {rejected[0]})"
    return None, "no unpublished videos left for this account's themes"


def next_local_midnight(account: Account,
                        now_utc: datetime | None = None) -> datetime:
    """Start of the account's next local day, in UTC.

    When a "not today" deferral expires. Measured in the account's own
    timezone, so "today" means the operator's day, not UTC's: deferring at
    23:50 IST frees the video ten minutes later, which is the literal reading
    of "not today" and the one that keeps it in rotation.
    """
    from .config import get_settings
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        tzinfo = ZoneInfo(account.timezone or get_settings().tz)
    except Exception:
        tzinfo = ZoneInfo("UTC")
    local = now_utc.astimezone(tzinfo)
    tomorrow = (local + timedelta(days=1)).date()
    return datetime.combine(tomorrow, time(0, 0), tzinfo=tzinfo).astimezone(
        timezone.utc)


def reserve_publication(session: Session, account: Account, video: Video, *,
                        scheduled_at: datetime | None = None) -> Publication:
    """Create the publication row that claims this video for this account.

    The unique constraint on (video, account, platform) is the real guard: if a
    concurrent scheduler wins the race, the insert fails rather than producing a
    second post of the same clip to the same account.

    That same constraint is why an expired "not today" deferral is revived in
    place rather than re-inserted: the video became a candidate again, but its
    old row still occupies the only slot the constraint allows.
    """
    revived = session.scalar(
        select(Publication).where(
            Publication.video_id == video.id,
            Publication.account_id == account.id,
            Publication.platform == account.platform,
            Publication.status == PublicationStatus.deferred,
        ).with_for_update()
    )
    if revived is not None:
        revived.status = PublicationStatus.pending
        revived.available_after = None
        revived.error_message = None
        revived.approved_by = None
        revived.approval_requested_at = None
        revived.approved_at = None
        # A revived row enters a NEW slot, so it must not carry yesterday's
        # attempt count in and shrink today's rejection budget.
        # propose_replacement overwrites this straight after when it is the
        # caller, so resetting here is safe.
        revived.proposal_attempt = 1
        revived.scheduled_at = scheduled_at or datetime.now(timezone.utc)
        revived.idempotency_key = uuid.uuid4().hex
        session.flush()
        log.info("revived deferred publication %s (video %s -> %s)",
                 revived.id, video.id, account.username)
        return revived

    publication = Publication(
        video_id=video.id,
        account_id=account.id,
        platform=account.platform,
        status=PublicationStatus.pending,
        scheduled_at=scheduled_at or datetime.now(timezone.utc),
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(publication)
    session.flush()
    return publication


def pending_count(session: Session, account: Account) -> int:
    """How many videos this account could still post - the /status number."""
    return session.scalar(
        select(func.count()).select_from(
            candidate_video_query(account, None)
            .where(Video.theme_id.in_(
                select(AccountTheme.theme_id).where(
                    AccountTheme.account_id == account.id,
                    AccountTheme.enabled.is_(True))))
            .subquery()
        )
    ) or 0


def published_in_window(session: Session, account: Account, hours: int = 24) -> int:
    """Platform rate limits are per rolling window, so this is what the
    scheduler checks before it queues anything."""
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    return session.scalar(
        select(func.count()).select_from(Publication).where(
            Publication.account_id == account.id,
            Publication.status == PublicationStatus.published,
            Publication.published_at.isnot(None),
            Publication.published_at >= since,
        )
    ) or 0


def propose_replacement(session: Session, rejected: Publication,
                        limit: int | None = None) -> tuple[Publication | None, str]:
    """After a rejection, offer a different video for the same slot.

    The budget is per slot: five rejected proposals means the slot is abandoned
    for the day rather than the system continuing to suggest videos indefinitely.
    The rejected row is kept so the video is never offered to this account again.
    """
    from .config import get_settings

    limit = limit or get_settings().rejection_limit
    account = session.get(Account, rejected.account_id)
    attempt = (rejected.proposal_attempt or 1) + 1

    if attempt > limit:
        return None, (f"{limit} proposals rejected for @{account.username} - "
                      f"stopping for this slot.")

    video, reason = select_next_video(session, account)
    if video is None:
        return None, f"No alternative video available: {reason}"

    replacement = reserve_publication(session, account, video,
                                      scheduled_at=rejected.scheduled_at)
    replacement.proposal_attempt = attempt
    session.flush()
    return replacement, (f"Proposal {attempt} of {limit} for "
                         f"@{account.username}.")


def coordinated_peers(session: Session, theme_id: int,
                      exclude_account_id: int | None = None) -> list[Account]:
    """Enabled accounts sharing a coordinated theme."""
    query = (
        select(Account)
        .join(AccountTheme, AccountTheme.account_id == Account.id)
        .where(AccountTheme.theme_id == theme_id,
               AccountTheme.enabled.is_(True),
               Account.enabled.is_(True))
        .order_by(Account.id)
    )
    if exclude_account_id is not None:
        query = query.where(Account.id != exclude_account_id)
    return list(session.scalars(query))


def video_is_eligible_for(session: Session, account: Account, video: Video
                          ) -> tuple[bool, str | None]:
    """Whether this exact video may be published by this account.

    Used by the coordinated fan-out, where the video is already chosen and the
    question is only whether each peer may take it.
    """
    existing = session.scalar(
        select(Publication).where(Publication.video_id == video.id,
                                  Publication.account_id == account.id,
                                  Publication.platform == account.platform))
    if existing is not None:
        return False, f"already has publication #{existing.id}"

    source_ids = account_source_ids(account)
    if source_ids is not None and video.source_id not in source_ids:
        return False, "video's source is not mapped to this account"

    theme_ids = {t.theme_id for t in eligible_themes(session, account)}
    if video.theme_id not in theme_ids:
        return False, "video's theme is not mapped to this account"

    mappings = list(session.scalars(
        select(VideoAccountMapping).where(VideoAccountMapping.video_id == video.id)))
    if mappings and not any(m.account_id == account.id and m.enabled
                            for m in mappings):
        return False, "video is pinned to other accounts"

    if video.metadata_status == MetadataStatus.ok:
        info = VideoInfo(duration=video.duration, width=video.width,
                         height=video.height, fps=video.fps, codec=video.codec,
                         file_size=video.file_size)
        ok, reason = check_compatibility(info, account.platform.value)
        if not ok:
            return False, reason
    return True, None
