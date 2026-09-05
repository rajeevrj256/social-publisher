"""The publish job itself, plus crash recovery.

Every state change is written to Postgres before and after the network call, so
a process that dies mid-upload leaves a row that recovery can reason about
instead of an invisible half-published post.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import session_scope
from .ffmpeg_utils import check_compatibility, probe
from .media import MediaUnavailable, materialize
from .metadata_ai import generate
from .models import (
    Account, MetadataStatus, Publication, PublicationStatus, Video,
)
from .notifications import publication_message, send_message
from .publishers import PublishError, RetryablePublishError, get_publisher

log = logging.getLogger(__name__)


def _absolute_path(video: Video) -> Path:
    return Path(get_settings().video_root) / video.filepath


def prepare_metadata(session: Session, publication: Publication) -> bool:
    """Fill in the metadata actually used and keep it on the publication row -
    regenerating later would lose what was really posted.

    Returns True when the text was genuinely authored (by a template or a
    model). False means only a filename fallback exists, and a human should
    supply the real wording before anything is published.
    """
    account = session.get(Account, publication.account_id)
    video = session.get(Video, publication.video_id)
    meta = generate(session, account, video)
    publication.title = publication.title or meta.title
    publication.caption = publication.caption or meta.caption
    publication.hashtags = publication.hashtags or meta.hashtags
    publication.description = publication.description or meta.description
    return meta.authored


def publish_publication(publication_id: int) -> str:
    """RQ entry point. Returns the terminal status as a string."""
    settings = get_settings()

    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            return "missing"
        if publication.status == PublicationStatus.published:
            return "already_published"  # idempotent: never post twice
        if publication.status == PublicationStatus.publishing:
            log.warning("publication %s already in flight", publication_id)
            return "in_flight"
        if publication.status in (PublicationStatus.awaiting_approval,
                                  PublicationStatus.awaiting_metadata,
                                  PublicationStatus.rejected):
            # The queue is not the authority on consent; the row is. A job that
            # somehow reaches the worker without approval stops here.
            log.warning("publication %s is %s - refusing to publish",
                        publication_id, publication.status.value)
            return publication.status.value

        account = session.get(Account, publication.account_id)
        video = session.get(Video, publication.video_id)
        if not account or not video:
            publication.status = PublicationStatus.failed_permanent
            publication.error_message = "account or video row missing"
            return publication.status.value

        prepare_metadata(session, publication)
        publication.status = PublicationStatus.publishing
        publication.claimed_at = datetime.now(timezone.utc)
        session.flush()

        account_id, video_id = account.id, video.id
        platform = account.platform.value

    try:
        with session_scope() as session:
            publication = session.get(Publication, publication_id)
            account = session.get(Account, account_id)
            video = session.get(Video, video_id)

            # Fetches from Drive when needed and deletes on the way out, so the
            # worker only ever holds one video at a time.
            with materialize(video) as file_path:
                if video.metadata_status != MetadataStatus.ok:
                    info = probe(file_path)
                    if info.ok:
                        video.duration, video.width, video.height = (
                            info.duration, info.width, info.height)
                        video.fps, video.file_size, video.codec = (
                            info.fps, info.file_size, info.codec)
                        video.metadata_status = MetadataStatus.ok
                    else:
                        video.metadata_status = MetadataStatus.invalid
                        video.metadata_error = info.error
                        raise PublishError(f"unreadable video: {info.error}")

                    ok, reason = check_compatibility(info, platform)
                    if not ok:
                        raise PublishError(f"not accepted by {platform}: {reason}")

                publisher = get_publisher(platform)
                publication.uploaded_at = datetime.now(timezone.utc)
                result = publisher.publish(account, video, publication,
                                           str(file_path))

            publication.platform_post_id = result.platform_post_id
            publication.platform_url = result.platform_url
            publication.published_at = datetime.now(timezone.utc)
            publication.status = PublicationStatus.published
            publication.error_message = None
            _notify(session, publication)
        return PublicationStatus.published.value

    except MediaUnavailable as exc:
        with session_scope() as session:
            publication = session.get(Publication, publication_id)
            publication.retry_count += 1
            publication.error_message = str(exc)[:1000]
            # A Drive hiccup is worth retrying; a missing local file is not.
            publication.status = (
                PublicationStatus.failed
                if publication.retry_count < settings.max_retries
                else PublicationStatus.failed_permanent)
            status = publication.status.value
            if status == PublicationStatus.failed_permanent.value:
                _notify(session, publication)
        return status

    except RetryablePublishError as exc:
        with session_scope() as session:
            publication = session.get(Publication, publication_id)
            publication.retry_count += 1
            publication.error_message = str(exc)[:1000]
            if publication.retry_count >= settings.max_retries:
                publication.status = PublicationStatus.failed_permanent
                _notify(session, publication)
            else:
                publication.status = PublicationStatus.failed
            status = publication.status.value
        log.warning("retryable failure on publication %s: %s", publication_id, exc)
        return status

    except PublishError as exc:
        with session_scope() as session:
            publication = session.get(Publication, publication_id)
            publication.status = PublicationStatus.failed_permanent
            publication.error_message = str(exc)[:1000]
            publication.retry_count += 1
            _notify(session, publication)
        log.error("permanent failure on publication %s: %s", publication_id, exc)
        return PublicationStatus.failed_permanent.value

    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            publication = session.get(Publication, publication_id)
            publication.retry_count += 1
            publication.error_message = f"unexpected: {exc}"[:1000]
            publication.status = (
                PublicationStatus.failed_permanent
                if publication.retry_count >= settings.max_retries
                else PublicationStatus.failed)
            status = publication.status.value
        log.exception("unexpected failure on publication %s", publication_id)
        return status


def _notify(session: Session, publication: Publication) -> None:
    account = session.get(Account, publication.account_id)
    video = session.get(Video, publication.video_id)
    theme = video.theme.name if video and video.theme else "unknown"
    send_message(publication_message(publication, account, video, theme))


def recover_stuck_publications() -> int:
    """Reconcile rows left in `publishing` by a crash.

    A row with a platform id clearly succeeded before the process died. A row
    without one is ambiguous: it is parked in needs_review rather than retried,
    because a blind retry is how the same reel ends up posted twice.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.stuck_job_minutes)
    recovered = 0

    with session_scope() as session:
        stuck = list(session.scalars(
            select(Publication).where(
                Publication.status == PublicationStatus.publishing,
                Publication.claimed_at.isnot(None),
                Publication.claimed_at < cutoff,
            )
        ))
        for publication in stuck:
            if publication.platform_post_id:
                publication.status = PublicationStatus.published
                publication.published_at = (publication.published_at
                                            or datetime.now(timezone.utc))
                publication.error_message = "recovered after restart"
            else:
                publication.status = PublicationStatus.needs_review
                publication.error_message = (
                    "process died mid-upload; verify on the platform before retrying")
            recovered += 1

        if recovered:
            send_message(f"♻️ Crash recovery: {recovered} stuck publication(s) "
                         f"reconciled. Check /failed for anything needing review.")
    return recovered


def index_source(source_id: int) -> dict:
    """Index a source's folder as a background job.

    Listing a Drive folder takes minutes for a few hundred files, far too long
    to hold an HTTP request open, so the API queues this instead.
    """
    from .models import Source, SourceKind, Theme
    from .scanner import scan_gdrive_source, scan

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            return {"error": f"source {source_id} not found"}

        theme = None
        if source.videos_theme_id:
            theme = session.get(Theme, source.videos_theme_id)

        if source.kind == SourceKind.gdrive:
            stats = scan_gdrive_source(session, source, theme)
        else:
            from pathlib import Path

            from .config import get_settings
            root = Path(get_settings().video_root) / source.location
            stats = scan(session, str(root), source_id=source.id,
                         default_theme_id=theme.id if theme else None)

        log.info("indexed source %s: %s", source.name, stats)
        send_message(
            f"📥 Indexed {source.name}\n\n"
            f"Theme: {theme.name if theme else 'unassigned'}\n"
            f"Added: {stats.get('added', 0)}\n"
            f"Already known: {stats.get('skipped', 0) + stats.get('updated', 0)}\n"
            f"Total seen: {stats.get('seen', 0)}")
        return stats
