"""Per-account scheduling.

Each account is evaluated on its own: its own schedule rows, its own themes, its
own queue. One account exhausting its library or hitting a platform limit has no
effect on any other.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import advisory_lock, session_scope
import uuid

from .models import (
    Account, AccountSchedule, Platform, Publication, PublicationStatus,
    Schedule, ScheduleAccount, Theme, Video,
)
from .jobs import prepare_metadata, request_decision
from .metadata_ai import metadata_complete
from .notifications import (
    send_approval_request, send_message, send_metadata_request,
)
from .selection import (
    coordinated_peers, next_local_midnight, published_in_window,
    reserve_publication, select_next_video, video_is_eligible_for,
)

log = logging.getLogger(__name__)


def account_timezone(account: Account, schedule: AccountSchedule | None = None):
    name = (schedule.timezone if schedule and schedule.timezone
            else account.timezone) or get_settings().tz
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def due_slots(session: Session, account: Account, now_utc: datetime | None = None,
              grace_minutes: int = 30) -> list[datetime]:
    """Slot datetimes that are due but not yet filled.

    The grace window matters: a worker restart or a minute of downtime should
    still publish the 10:00 slot at 10:05 rather than silently skipping the day.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    schedules = list(session.scalars(
        select(AccountSchedule).where(
            AccountSchedule.account_id == account.id,
            AccountSchedule.enabled.is_(True),
        )
    ))
    slots: list[datetime] = []

    for schedule in schedules:
        tzinfo = account_timezone(account, schedule)
        local_now = now_utc.astimezone(tzinfo)
        # Check yesterday too: a slot late in the local day can still be inside
        # the grace window after midnight UTC rolls over.
        for day_offset in (0, -1):
            local_day = (local_now + timedelta(days=day_offset)).date()
            if (schedule.day_of_week is not None
                    and local_day.weekday() != schedule.day_of_week):
                continue
            slot_local = datetime.combine(local_day, schedule.publish_time,
                                          tzinfo=tzinfo)
            slot_utc = slot_local.astimezone(timezone.utc)
            if slot_utc > now_utc:
                continue
            if now_utc - slot_utc > timedelta(minutes=grace_minutes):
                continue
            for index in range(schedule.videos_per_day):
                slots.append(slot_utc + timedelta(seconds=index))

    return sorted(set(slots))


def slot_already_filled(session: Session, account: Account,
                        slot: datetime) -> bool:
    """One publication per slot. Matching on the exact scheduled_at is what
    stops a tick every 60 seconds from queueing the same slot repeatedly."""
    return session.scalar(
        select(func.count()).select_from(Publication).where(
            Publication.account_id == account.id,
            Publication.scheduled_at == slot,
        )
    ) > 0


def platform_limit_reached(session: Session, account: Account) -> bool:
    settings = get_settings()
    limit = (settings.instagram_daily_limit
             if account.platform == Platform.instagram
             else settings.youtube_daily_limit)
    return published_in_window(session, account, hours=24) >= limit


def schedule_account(session: Session, account: Account, *,
                     now_utc: datetime | None = None,
                     enqueue: bool = True) -> list[int]:
    """Reserve and queue everything this account owes right now."""
    created: list[int] = []
    awaiting: list[int] = []
    if not account.enabled:
        return created

    slots = due_slots(session, account, now_utc)
    if not slots:
        return created

    if platform_limit_reached(session, account):
        log.info("@%s at its 24h platform limit; skipping", account.username)
        return created

    # Serialise per account so two scheduler replicas cannot fill one slot twice.
    with advisory_lock(session, account.id):
        for slot in slots:
            if slot_already_filled(session, account, slot):
                continue
            video, reason = select_next_video(session, account)
            if video is None:
                log.info("@%s has nothing to post: %s", account.username, reason)
                break
            publication = reserve_publication(session, account, video,
                                              scheduled_at=slot)
            # Coordinated theme: the same video goes to every other account on
            # it, chosen once here rather than each account picking its own.
            peers_created = []
            theme = session.get(Theme, video.theme_id) if video.theme_id else None
            if theme is not None and theme.coordinated:
                group = uuid.uuid4().hex
                publication.group_key = group
                for peer in coordinated_peers(session, theme.id,
                                              exclude_account_id=account.id):
                    ok, why = video_is_eligible_for(session, peer, video)
                    if not ok:
                        log.info("coordinated skip @%s for %s: %s",
                                 peer.username, video.filename, why)
                        continue
                    mirror = reserve_publication(session, peer, video,
                                                 scheduled_at=slot)
                    mirror.group_key = group
                    peers_created.append(mirror)
                session.flush()
            # Metadata is generated now, not at upload time: the approval
            # message has to show the exact caption that will go out, and the
            # human's yes has to bind to that text.
            authored = prepare_metadata(session, publication)
            if account.require_approval:
                theme_name = video.theme.name if video.theme else "unknown"
                missing = metadata_complete(publication)
                publication.approval_requested_at = datetime.now(timezone.utc)
                if peers_created:
                    extra = ", ".join(f"@{session.get(Account, p.account_id).username}"
                                      for p in peers_created)
                    log.info("coordinated group %s covers %s + %s",
                             publication.group_key, account.username, extra)
                # Nothing real may have been written for this post; ask
                # rather than publish a filename as a caption.
                request_decision(session, publication, account, video,
                                 theme_name)
                awaiting.append(publication.id)

                # Each peer gets its OWN metadata and its OWN approval: the
                # video is shared, the wording is not. One account's caption
                # must never be published under another's name.
                for mirror in peers_created:
                    peer = session.get(Account, mirror.account_id)
                    if not peer.require_approval:
                        mirror.status = PublicationStatus.queued
                        session.flush()
                        continue
                    request_decision(session, mirror, peer, video, theme_name)
                    awaiting.append(mirror.id)
            else:
                # Unattended account: queue directly, and let each peer follow
                # its own approval policy rather than inheriting this one.
                publication.status = PublicationStatus.queued
                session.flush()
                created.append(publication.id)
                for mirror in peers_created:
                    peer = session.get(Account, mirror.account_id)
                    if peer.require_approval:
                        request_decision(session, mirror, peer, video)
                        awaiting.append(mirror.id)
                    else:
                        mirror.status = PublicationStatus.queued
                        session.flush()
                        created.append(mirror.id)

    if enqueue and created:
        from .worker import get_queue
        queue = get_queue()
        for publication_id in created:
            queue.enqueue("src.jobs.publish_publication", publication_id,
                          job_timeout=3600, result_ttl=86400)
    return created


def retry_failed(session: Session, *, enqueue: bool = True) -> list[int]:
    """Re-queue retryable failures, independently per account and platform:
    a YouTube success must never cause an Instagram retry to be skipped."""
    settings = get_settings()
    ready = list(session.scalars(
        select(Publication).where(
            Publication.status == PublicationStatus.failed,
            Publication.retry_count < settings.max_retries,
        ).limit(50)
    ))
    ids = []
    for publication in ready:
        publication.status = PublicationStatus.queued
        ids.append(publication.id)
    session.flush()

    if enqueue and ids:
        from .worker import get_queue
        queue = get_queue()
        for publication_id in ids:
            queue.enqueue("src.jobs.publish_publication", publication_id,
                          job_timeout=3600, result_ttl=86400)
    return ids


def approval_deadline(account: Account, requested_at: datetime,
                      cutoff_hour: int | None = None) -> datetime:
    """The first cutoff hour, account-local, strictly after the request.

    Expressed as a deadline rather than "run a cron at 23:00" on purpose: a
    cron fires once and a container that is down at 23:00 misses it forever,
    leaving the request stranded. A deadline is re-evaluated on every tick, so
    a restart at 23:40 still expires it.
    """
    settings = get_settings()
    cutoff_hour = settings.approval_cutoff_hour if cutoff_hour is None else cutoff_hour
    try:
        tzinfo = ZoneInfo(account.timezone or settings.tz)
    except Exception:
        tzinfo = ZoneInfo("UTC")
    local = requested_at.astimezone(tzinfo)
    deadline = datetime.combine(local.date(), time(cutoff_hour, 0), tzinfo=tzinfo)
    if deadline <= local:
        deadline += timedelta(days=1)
    return deadline.astimezone(timezone.utc)


def expire_stale_approvals(session: Session,
                           now_utc: datetime | None = None) -> list[int]:
    """Defer approval requests nobody answered before the cutoff.

    Silence is not a rejection. The slot is lost, but the video goes back in
    the pool for the next day rather than being burned by inaction.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    expired: list[int] = []
    waiting = session.scalars(
        select(Publication).where(
            Publication.status.in_([PublicationStatus.awaiting_approval,
                                    PublicationStatus.awaiting_metadata]))
    )
    for publication in waiting:
        account = session.get(Account, publication.account_id)
        if account is None:
            continue
        # A row with no timestamp has no deadline to measure from; treat the
        # moment it was scheduled as the request instead of skipping it, or it
        # would wait forever.
        requested = (publication.approval_requested_at
                     or publication.scheduled_at or publication.created_at)
        if requested is None:
            continue
        if requested.tzinfo is None:
            requested = requested.replace(tzinfo=timezone.utc)
        if now_utc < approval_deadline(account, requested):
            continue

        publication.status = PublicationStatus.deferred
        publication.available_after = next_local_midnight(account, now_utc)
        publication.error_message = "no answer before cutoff - deferred"
        session.flush()
        expired.append(publication.id)
        video = session.get(Video, publication.video_id)
        log.info("auto-deferred publication %s (@%s) - no answer by cutoff",
                 publication.id, account.username)
        send_message(
            f"🕒 No answer for #{publication.id} "
            f"({video.filename if video else 'video'} -> @{account.username}).\n"
            f"Auto-deferred. Back in the pool tomorrow.")
    return expired


def tick() -> dict:
    """One scheduler pass across every enabled account."""
    summary = {"schedules": 0, "accounts": 0, "queued": 0, "retried": 0,
               "expired": 0}
    with session_scope() as session:
        # Shared schedules first: they own their members, and a member must not
        # then be scheduled again by the per-account pass.
        for schedule in session.scalars(
                select(Schedule).where(Schedule.enabled.is_(True))):
            summary["schedules"] += 1
            try:
                summary["queued"] += len(run_schedule(session, schedule))
            except Exception:
                log.exception("shared schedule %s failed", schedule.name)
                session.rollback()

        owned = accounts_in_shared_schedules(session)
        accounts = list(session.scalars(
            select(Account).where(Account.enabled.is_(True))))
        for account in accounts:
            if account.id in owned:
                continue
            summary["accounts"] += 1
            try:
                summary["queued"] += len(schedule_account(session, account))
            except Exception:
                log.exception("scheduling failed for @%s", account.username)
                session.rollback()
        try:
            summary["retried"] = len(retry_failed(session))
        except Exception:
            log.exception("retry sweep failed")
        try:
            summary["expired"] = len(expire_stale_approvals(session))
        except Exception:
            log.exception("approval expiry sweep failed")
            session.rollback()
    return summary


def main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from .jobs import recover_stuck_publications
    from .logging_setup import setup_logging
    from .telegram_bot import send_daily_report

    settings = get_settings()
    log_ = setup_logging("scheduler")
    recover_stuck_publications()

    scheduler = BlockingScheduler(timezone=settings.tz)
    scheduler.add_job(tick, "interval", seconds=settings.scheduler_tick_seconds,
                      id="tick", max_instances=1, coalesce=True)
    scheduler.add_job(recover_stuck_publications, "interval", minutes=15,
                      id="recovery", max_instances=1)
    scheduler.add_job(send_daily_report, "cron", hour=23, minute=55,
                      id="daily_report")
    log_.info("scheduler started (tick every %ss)", settings.scheduler_tick_seconds)
    scheduler.start()




def schedule_slots(session: Session, schedule, now_utc: datetime | None = None,
                   grace_minutes: int = 30) -> list[datetime]:
    """Due slots for a shared schedule, in its own timezone."""
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        tzinfo = ZoneInfo(schedule.timezone or get_settings().tz)
    except Exception:
        tzinfo = ZoneInfo("UTC")

    local_now = now_utc.astimezone(tzinfo)
    slots: list[datetime] = []
    for day_offset in (0, -1):
        local_day = (local_now + timedelta(days=day_offset)).date()
        if (schedule.day_of_week is not None
                and local_day.weekday() != schedule.day_of_week):
            continue
        slot_local = datetime.combine(local_day, schedule.publish_time,
                                      tzinfo=tzinfo)
        slot_utc = slot_local.astimezone(timezone.utc)
        if slot_utc > now_utc:
            continue
        if now_utc - slot_utc > timedelta(minutes=grace_minutes):
            continue
        for index in range(schedule.videos_per_day):
            slots.append(slot_utc + timedelta(seconds=index))
    return sorted(set(slots))


def schedule_members(session: Session, schedule) -> list[Account]:
    return list(session.scalars(
        select(Account)
        .join(ScheduleAccount, ScheduleAccount.account_id == Account.id)
        .where(ScheduleAccount.schedule_id == schedule.id,
               ScheduleAccount.enabled.is_(True),
               Account.enabled.is_(True))
        .order_by(Account.id)))


def run_schedule(session: Session, schedule, *, now_utc: datetime | None = None,
                 enqueue: bool = True) -> list[int]:
    """Fire one shared slot: pick a video once, give it to every member.

    This is what makes "same video everywhere" hold. Per-account schedules
    cannot: they fire at their own times, and each fan-out reaches the others,
    so every account ends up with a publication per member schedule.
    """
    created: list[int] = []
    if not schedule.enabled:
        return created

    members = schedule_members(session, schedule)
    if not members:
        log.info("schedule %s has no enabled accounts", schedule.name)
        return created

    slots = schedule_slots(session, schedule, now_utc)
    if not slots:
        return created

    # One lock for the whole schedule, so two replicas cannot both fire it.
    with advisory_lock(session, 10_000_000 + schedule.id):
        for slot in slots:
            already = session.scalar(
                select(func.count()).select_from(Publication).where(
                    Publication.account_id.in_([m.id for m in members]),
                    Publication.scheduled_at == slot))
            if already:
                continue

            chooser = next((m for m in members
                            if not platform_limit_reached(session, m)), None)
            if chooser is None:
                log.info("every account in %s is at its platform limit",
                         schedule.name)
                break

            video, reason = select_next_video(session, chooser,
                                              theme_id=schedule.theme_id)
            if video is None:
                log.info("schedule %s has nothing to post: %s",
                         schedule.name, reason)
                break

            group = uuid.uuid4().hex
            for member in members:
                if platform_limit_reached(session, member):
                    log.info("skipping @%s: at its 24h platform limit",
                             member.username)
                    continue
                ok, why = video_is_eligible_for(session, member, video)
                if not ok:
                    log.info("skipping @%s for %s: %s", member.username,
                             video.filename, why)
                    continue
                publication = reserve_publication(session, member, video,
                                                  scheduled_at=slot)
                publication.group_key = group
                session.flush()

                theme_name = video.theme.name if video.theme else "unknown"
                if member.require_approval:
                    request_decision(session, publication, member, video,
                                     theme_name)
                else:
                    publication.status = PublicationStatus.queued
                    session.flush()
                    created.append(publication.id)

    if enqueue and created:
        from .worker import get_queue
        queue = get_queue()
        for publication_id in created:
            queue.enqueue("src.jobs.publish_publication", publication_id,
                          job_timeout=3600, result_ttl=86400)
    return created


def accounts_in_shared_schedules(session: Session) -> set[int]:
    """Accounts owned by a shared schedule.

    They must be skipped by the per-account pass, or the same account is
    scheduled twice for the same day.
    """
    return set(session.scalars(
        select(ScheduleAccount.account_id)
        .join(Schedule, Schedule.id == ScheduleAccount.schedule_id)
        .where(ScheduleAccount.enabled.is_(True), Schedule.enabled.is_(True))))


if __name__ == "__main__":
    main()
