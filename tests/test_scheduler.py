"""Per-account scheduling: slots, independence, and platform limits."""

from datetime import datetime, time, timedelta, timezone

from src.models import (
    Account, AccountSchedule, AccountTheme, Platform, Publication,
    PublicationStatus,
)
from src.scheduler import due_slots, platform_limit_reached, slot_already_filled
from src.selection import reserve_publication, select_next_video
from tests.conftest import make_account, make_video


def _schedule(session, account, hour, minute=0, per_day=1, day=None):
    row = AccountSchedule(account_id=account.id, publish_time=time(hour, minute),
                          videos_per_day=per_day, day_of_week=day,
                          timezone="UTC")
    session.add(row)
    session.flush()
    return row


def test_slot_is_due_within_the_grace_window(session, themes):
    account = make_account(session, "puncty")
    account.timezone = "UTC"
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    _schedule(session, account, 10, 0)

    slots = due_slots(session, account, now)
    assert len(slots) == 1
    assert slots[0].hour == 10


def test_future_slot_is_not_due(session, themes):
    account = make_account(session, "future")
    account.timezone = "UTC"
    now = datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc)
    _schedule(session, account, 10, 0)
    assert due_slots(session, account, now) == []


def test_stale_slot_is_skipped_not_published_late(session, themes):
    """A slot missed by hours should not fire at 3am; the day is simply lost."""
    account = make_account(session, "stale")
    account.timezone = "UTC"
    now = datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)
    _schedule(session, account, 10, 0)
    assert due_slots(session, account, now) == []


def test_two_videos_per_day_yields_two_slots(session, themes):
    account = make_account(session, "busy")
    account.timezone = "UTC"
    now = datetime(2026, 9, 5, 12, 10, tzinfo=timezone.utc)
    _schedule(session, account, 12, 0, per_day=2)
    assert len(due_slots(session, account, now)) == 2


def test_weekday_restriction_is_respected(session, themes):
    account = make_account(session, "weekdays")
    account.timezone = "UTC"
    # 5 Sep 2026 is a Saturday (weekday 5).
    saturday = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    _schedule(session, account, 10, 0, day=0)  # Mondays only
    assert due_slots(session, account, saturday) == []


def test_filled_slot_is_not_scheduled_twice(session, themes):
    """The scheduler ticks every minute; one slot must produce one post."""
    account = make_account(session, "once")
    session.add(AccountTheme(account_id=account.id,
                             theme_id=themes["Motivation"].id, priority=10))
    session.flush()
    video = make_video(session, themes["Motivation"], "a.mp4")
    slot = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)

    assert slot_already_filled(session, account, slot) is False
    reserve_publication(session, account, video, scheduled_at=slot)
    assert slot_already_filled(session, account, slot) is True


def test_platform_limit_blocks_further_posts(session, themes):
    account = make_account(session, "limited", Platform.instagram)
    session.add(AccountTheme(account_id=account.id,
                             theme_id=themes["Motivation"].id))
    session.flush()

    assert platform_limit_reached(session, account) is False

    now = datetime.now(timezone.utc)
    for index in range(100):
        video = make_video(session, themes["Motivation"], f"v{index}.mp4")
        publication = reserve_publication(session, account, video)
        publication.status = PublicationStatus.published
        publication.published_at = now - timedelta(minutes=index)
    session.flush()

    assert platform_limit_reached(session, account) is True, \
        "Instagram allows 100 API publishes per rolling 24h"


def test_accounts_schedule_independently(session, themes):
    """One account running dry must not affect another."""
    empty = make_account(session, "empty_library")
    stocked = make_account(session, "stocked")
    for account in (empty, stocked):
        account.timezone = "UTC"
        session.add(AccountTheme(account_id=account.id,
                                 theme_id=themes["Motivation"].id))
    session.flush()
    make_video(session, themes["Motivation"], "only.mp4")

    first, _ = select_next_video(session, empty)
    reserve_publication(session, empty, first)

    second, reason = select_next_video(session, stocked)
    assert second is not None, f"other account must be unaffected: {reason}"
