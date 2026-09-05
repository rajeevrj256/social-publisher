"""A "not today" rejection must return the video, a permanent one must not.

The distinction lives in two places that have to agree: candidate_video_query
(which videos are offered) and reserve_publication (which row claims one). If
only the query let an expired deferral through, the reservation would hit the
unique constraint on (video, account, platform) and raise instead of posting.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.models import Account, AccountTheme, Publication, PublicationStatus, Video
from src.selection import (candidate_video_query, reserve_publication,
                           select_next_video)
from src.telegram_bot import next_local_midnight
from tests.conftest import make_account, make_video


@pytest.fixture
def setup(session, themes):
    account = make_account(session, "acct1")
    theme = themes["Success"]
    session.add(AccountTheme(account_id=account.id, theme_id=theme.id,
                             enabled=True))
    videos = [make_video(session, theme, f"v{i}.mp4") for i in range(3)]
    session.flush()
    return account, theme, videos


def candidates(session, account):
    query = candidate_video_query(account, None)
    return set(session.scalars(
        select(Video.id).from_statement(query.with_only_columns(Video.id))))


def test_permanent_rejection_never_returns(session, setup):
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.rejected
    session.flush()

    assert videos[0].id not in candidates(session, account)


def test_deferral_blocks_today(session, setup):
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.deferred
    pub.available_after = datetime.now(timezone.utc) + timedelta(hours=6)
    session.flush()

    assert videos[0].id not in candidates(session, account), \
        "a deferral still in force must not be offered again the same day"


def test_deferral_returns_after_expiry(session, setup):
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.deferred
    pub.available_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.flush()

    assert videos[0].id in candidates(session, account), \
        "once available_after passes the video must be selectable again"


def test_expired_deferral_is_revived_not_reinserted(session, setup):
    """The unique constraint allows one row, so revival must reuse it."""
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    original_id = pub.id
    pub.status = PublicationStatus.deferred
    pub.available_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    pub.proposal_attempt = 4
    session.flush()

    again = reserve_publication(session, account, videos[0])

    assert again.id == original_id, "revival must not create a second row"
    assert again.status == PublicationStatus.pending
    assert again.available_after is None
    assert again.proposal_attempt == 1, \
        "a new slot must start with a full rejection budget"
    rows = session.scalars(
        select(Publication).where(Publication.video_id == videos[0].id)).all()
    assert len(rows) == 1


def test_deferred_video_can_be_picked_by_scheduler_next_day(session, setup):
    """End to end: defer every other candidate, expire the deferral, and the
    selector must hand back the deferred video rather than reporting none."""
    account, _, videos = setup
    for video in videos[1:]:
        blocked = reserve_publication(session, account, video)
        blocked.status = PublicationStatus.rejected
    deferred = reserve_publication(session, account, videos[0])
    deferred.status = PublicationStatus.deferred
    deferred.available_after = datetime.now(timezone.utc) - timedelta(minutes=1)
    session.flush()

    picked, reason = select_next_video(session, account)

    assert picked is not None, f"expected the revived video, got: {reason}"
    assert picked.id == videos[0].id


def test_next_local_midnight_is_tomorrow_in_account_timezone(session):
    account = make_account(session, "acct_tz")
    account.timezone = "Asia/Kolkata"
    # 18:30 UTC == 00:00 IST the next day, so a defer at 18:29 UTC (23:59 IST)
    # must free the video one minute later, not 24 hours later.
    now = datetime(2026, 9, 5, 18, 29, tzinfo=timezone.utc)

    back = next_local_midnight(account, now)

    assert back - now == timedelta(minutes=1)


def test_next_local_midnight_survives_bad_timezone(session):
    account = make_account(session, "acct_bad_tz")
    account.timezone = "Not/AZone"
    now = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)

    assert next_local_midnight(account, now) > now


# ---- Telegram wiring --------------------------------------------------------

def test_every_button_payload_reaches_some_registered_handler():
    """A button whose payload no registered pattern matches does nothing.

    The 'Fill in details' button shipped broken exactly this way, so every
    payload the real keyboards emit is checked against every pattern actually
    registered -- not against a hand-written copy of either.
    """
    import inspect
    import re

    from src import notifications, telegram_bot

    source = inspect.getsource(telegram_bot.main)
    patterns = [re.compile(m) for m in re.findall(r'pattern=r"([^"]+)"', source)]
    assert len(patterns) >= 2, "expected the approval and edit patterns"

    payloads = [b["callback_data"]
                for kb in (notifications.approval_keyboard(42),
                           notifications.metadata_keyboard(42))
                for row in kb["inline_keyboard"] for b in row]
    for expected in ("defer:42", "edit:42:title", "reject:42", "approve:42"):
        assert expected in payloads, f"no button emits {expected}"
    for payload in payloads:
        assert any(p.match(payload) for p in patterns), \
            f"{payload} would never reach a handler"


def test_send_approval_request_uses_that_keyboard():
    """Guards the seam: approval_keyboard is only meaningful if it is the one
    actually sent."""
    import inspect

    from src import notifications

    source = inspect.getsource(notifications.send_approval_request)
    assert "approval_keyboard(" in source


def test_defer_command_is_registered():
    import inspect

    from src import telegram_bot

    source = inspect.getsource(telegram_bot.main)
    assert '("defer", cmd_defer)' in source, "/defer is not wired to a handler"


# ---- auto-defer on silence --------------------------------------------------

def test_unanswered_approval_is_deferred_after_cutoff(session, setup, monkeypatch):
    """Silence must not burn a video. The slot is lost, the video is not."""
    from src import scheduler

    monkeypatch.setattr(scheduler, "send_message", lambda *a, **k: True,
                        raising=False)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    # Requested 10:00 IST; by 23:30 IST the cutoff has passed unanswered.
    pub.approval_requested_at = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    session.flush()

    expired = scheduler.expire_stale_approvals(
        session, datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc))

    assert expired == [pub.id]
    assert pub.status == PublicationStatus.deferred
    assert pub.available_after is not None
    assert videos[0].id not in candidates(session, account), \
        "still blocked until the deferral expires"


def test_approval_is_left_alone_before_cutoff(session, setup, monkeypatch):
    from src import scheduler

    monkeypatch.setattr(scheduler, "send_message", lambda *a, **k: True,
                        raising=False)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    pub.approval_requested_at = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    session.flush()

    # 20:30 IST -- still inside the day, the operator may yet answer.
    expired = scheduler.expire_stale_approvals(
        session, datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc))

    assert expired == []
    assert pub.status == PublicationStatus.awaiting_approval


def test_expiry_survives_a_restart_past_the_cutoff(session, setup, monkeypatch):
    """A cron at 23:00 that the container sleeps through never fires again.

    The deadline is recomputed every tick, so a process that only wakes at
    01:00 still expires yesterday's request.
    """
    from src import scheduler

    monkeypatch.setattr(scheduler, "send_message", lambda *a, **k: True,
                        raising=False)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    pub.approval_requested_at = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    session.flush()

    # 01:00 IST the following day: hours past the missed 23:00 cutoff.
    expired = scheduler.expire_stale_approvals(
        session, datetime(2026, 9, 5, 19, 30, tzinfo=timezone.utc))

    assert expired == [pub.id]


def test_awaiting_metadata_also_expires(session, setup, monkeypatch):
    """A request for a caption is just as unanswered as one for approval."""
    from src import scheduler

    monkeypatch.setattr(scheduler, "send_message", lambda *a, **k: True,
                        raising=False)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_metadata
    pub.approval_requested_at = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    session.flush()

    expired = scheduler.expire_stale_approvals(
        session, datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc))

    assert expired == [pub.id]


def test_auto_deferred_video_returns_the_next_day(session, setup, monkeypatch):
    """The whole point: tomorrow the selector offers it again."""
    from src import scheduler

    monkeypatch.setattr(scheduler, "send_message", lambda *a, **k: True,
                        raising=False)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    for video in videos[1:]:
        dead = reserve_publication(session, account, video)
        dead.status = PublicationStatus.rejected
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    pub.approval_requested_at = datetime(2026, 9, 5, 4, 30, tzinfo=timezone.utc)
    session.flush()

    scheduler.expire_stale_approvals(
        session, datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc))
    # Move the clock past the deferral rather than faking the column.
    assert pub.available_after == datetime(2026, 9, 5, 18, 30,
                                           tzinfo=timezone.utc), \
        "next IST midnight is 18:30 UTC"
    pub.available_after = datetime.now(timezone.utc) - timedelta(seconds=1)
    session.flush()

    picked, reason = select_next_video(session, account)

    assert picked is not None, f"expected the video back, got: {reason}"
    assert picked.id == videos[0].id


def test_expiry_runs_inside_tick():
    """Wiring, not logic: a sweep never called is a sweep that does nothing."""
    import inspect

    from src import scheduler

    assert "expire_stale_approvals" in inspect.getsource(scheduler.tick)


def test_decide_defer_sets_the_row_and_offers_another(session, setup, monkeypatch):
    """The button path end to end: defer this one, propose the next."""
    from src import jobs, telegram_bot

    sent = []
    # The send now happens inside jobs.request_decision, shared with the
    # scheduler, so that is where it must be intercepted.
    monkeypatch.setattr(jobs, "send_approval_request",
                        lambda *a, **k: sent.append(("approval", a)) or True)
    monkeypatch.setattr(jobs, "send_metadata_request",
                        lambda *a, **k: sent.append(("metadata", a)) or True)
    account, _, videos = setup
    account.timezone = "Asia/Kolkata"
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    session.flush()

    message = telegram_bot._decide(session, pub.id, "defer", "tester")

    assert pub.status == PublicationStatus.deferred
    assert pub.available_after is not None
    assert "Not today" in message
    assert videos[0].id not in candidates(session, account)
    assert sent, "a replacement video should have been offered for the slot"


def test_replacement_with_no_caption_is_asked_for_as_a_form(session, setup,
                                                            monkeypatch):
    """The bug: a replacement always arrived as an approval card, so a post
    with no caption offered no way to write one."""
    from src import jobs, telegram_bot

    sent = []
    monkeypatch.setattr(jobs, "send_approval_request",
                        lambda *a, **k: sent.append("approval") or True)
    monkeypatch.setattr(jobs, "send_metadata_request",
                        lambda *a, **k: sent.append("metadata") or True)
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    session.flush()

    telegram_bot._decide(session, pub.id, "reject", "tester")

    assert sent == ["metadata"], \
        "a replacement with nothing written must arrive as a form"


def test_double_tap_does_not_offer_two_videos(session, setup, monkeypatch):
    """Two replacements for one slot: what a second tap used to produce."""
    from src import jobs, telegram_bot
    from src.models import Publication

    sent = []
    monkeypatch.setattr(jobs, "send_approval_request",
                        lambda *a, **k: sent.append("approval") or True)
    monkeypatch.setattr(jobs, "send_metadata_request",
                        lambda *a, **k: sent.append("metadata") or True)
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    session.flush()

    telegram_bot._decide(session, pub.id, "reject", "tester")
    before = session.scalars(select(Publication)).all()
    second = telegram_bot._decide(session, pub.id, "reject", "tester")
    after = session.scalars(select(Publication)).all()

    assert len(after) == len(before), "a second tap created a second proposal"
    assert "already rejected" in second
    assert len(sent) == 1, "only one replacement message may be sent"


def test_double_tap_defer_is_also_guarded(session, setup, monkeypatch):
    from src import jobs, telegram_bot
    from src.models import Publication

    monkeypatch.setattr(jobs, "send_approval_request", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "send_metadata_request", lambda *a, **k: True)
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    session.flush()

    telegram_bot._decide(session, pub.id, "defer", "tester")
    before = len(session.scalars(select(Publication)).all())
    telegram_bot._decide(session, pub.id, "defer", "tester")

    assert len(session.scalars(select(Publication)).all()) == before


def test_deferred_post_can_still_be_approved(session, setup, monkeypatch):
    """A mistaken 'Not today' must be undoable."""
    from src import jobs, telegram_bot

    monkeypatch.setattr(jobs, "send_approval_request", lambda *a, **k: True)
    monkeypatch.setattr(jobs, "send_metadata_request", lambda *a, **k: True)
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    pub.title, pub.caption, pub.hashtags = "T", "C", "#t"
    session.flush()

    telegram_bot._decide(session, pub.id, "defer", "tester")
    assert pub.status == PublicationStatus.deferred

    message = telegram_bot._decide(session, pub.id, "approve", "tester")

    assert pub.status == PublicationStatus.queued, message
    assert pub.available_after is None, "approving must clear the deferral"


def test_decide_reject_is_still_permanent(session, setup, monkeypatch):
    """Guards the else-branch: adding defer must not soften reject."""
    from src import telegram_bot

    monkeypatch.setattr(telegram_bot, "send_approval_request", lambda *a, **k: True)
    monkeypatch.setattr(telegram_bot, "prepare_metadata", lambda *a, **k: None)
    account, _, videos = setup
    pub = reserve_publication(session, account, videos[0])
    pub.status = PublicationStatus.awaiting_approval
    session.flush()

    telegram_bot._decide(session, pub.id, "reject", "tester")

    assert pub.status == PublicationStatus.rejected
    assert pub.available_after is None
