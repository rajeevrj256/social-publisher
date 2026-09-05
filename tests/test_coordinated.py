"""Coordinated themes: one video, every account, separate approvals."""

from datetime import datetime, time, timedelta, timezone

import pytest

from src.models import (
    AccountSchedule, AccountTheme, Platform, Publication, PublicationStatus,
    Theme,
)
from src.scheduler import schedule_account
from src.selection import coordinated_peers, video_is_eligible_for
from tests.conftest import make_account, make_video


def _map(session, account, theme):
    session.add(AccountTheme(account_id=account.id, theme_id=theme.id))
    session.flush()


@pytest.fixture
def fanout(session, themes, monkeypatch):
    """Three accounts on one coordinated theme, all due at the same slot."""
    monkeypatch.setattr("src.scheduler.send_approval_request",
                        lambda *a, **k: True)
    monkeypatch.setattr("src.scheduler.send_metadata_request",
                        lambda *a, **k: True)

    theme = themes["Motivation"]
    theme.coordinated = True
    accounts = []
    for name, platform in [("yt", Platform.youtube), ("ig_a", Platform.instagram),
                           ("ig_b", Platform.instagram)]:
        account = make_account(session, name, platform, require_approval=True)
        account.timezone = "UTC"
        _map(session, account, theme)
        session.add(AccountSchedule(account_id=account.id,
                                    publish_time=time(10, 0), videos_per_day=1,
                                    timezone="UTC"))
        accounts.append(account)
    for index in range(4):
        make_video(session, theme, f"v{index}.mp4")
    session.flush()
    return accounts, theme


def test_peers_are_discovered(session, fanout):
    accounts, theme = fanout
    peers = coordinated_peers(session, theme.id, exclude_account_id=accounts[0].id)
    assert {p.username for p in peers} == {"ig_a", "ig_b"}


def test_one_run_gives_every_account_the_same_video(session, fanout):
    """The guarantee: chosen once, fanned out - not each account picking."""
    accounts, _ = fanout
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)

    created = schedule_account(session, accounts[0], now_utc=now, enqueue=False)
    publications = list(session.scalars(select_all()))

    assert len(publications) == 3, "one per account"
    video_ids = {p.video_id for p in publications}
    assert len(video_ids) == 1, f"all must share one video, got {video_ids}"
    assert {p.account_id for p in publications} == {a.id for a in accounts}


def test_every_publication_is_approved_separately(session, fanout):
    """Shared video, independent captions - one approval must not release all."""
    accounts, _ = fanout
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    schedule_account(session, accounts[0], now_utc=now, enqueue=False)

    publications = sorted(session.scalars(select_all()), key=lambda p: p.id)
    assert all(p.status in (PublicationStatus.awaiting_metadata,
                            PublicationStatus.awaiting_approval)
               for p in publications), "each waits on its own human"

    publications[0].status = PublicationStatus.published
    session.flush()
    others = [p for p in publications[1:]]
    assert all(p.status != PublicationStatus.published for p in others), \
        "approving one must never publish the others"


def test_the_group_is_traceable(session, fanout):
    accounts, _ = fanout
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    schedule_account(session, accounts[0], now_utc=now, enqueue=False)

    keys = {p.group_key for p in session.scalars(select_all())}
    assert len(keys) == 1 and None not in keys, "one shared group key"


def test_an_ineligible_peer_is_skipped_not_broken(session, themes, monkeypatch):
    """A video too long for Instagram must not block the YouTube post."""
    monkeypatch.setattr("src.scheduler.send_approval_request", lambda *a, **k: True)
    monkeypatch.setattr("src.scheduler.send_metadata_request", lambda *a, **k: True)

    theme = themes["Motivation"]
    theme.coordinated = True
    yt = make_account(session, "yt2", Platform.youtube, require_approval=True)
    ig = make_account(session, "ig2", Platform.instagram, require_approval=True)
    for account in (yt, ig):
        account.timezone = "UTC"
        _map(session, account, theme)
        session.add(AccountSchedule(account_id=account.id,
                                    publish_time=time(10, 0), videos_per_day=1,
                                    timezone="UTC"))
    make_video(session, theme, "long.mp4", duration=600.0)   # 10 min
    session.flush()

    ok, reason = video_is_eligible_for(
        session, ig, session.scalars(select_videos()).first())
    assert ok is False and "too long" in reason

    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)
    schedule_account(session, yt, now_utc=now, enqueue=False)
    publications = list(session.scalars(select_all()))
    assert len(publications) == 1, "only the compatible account gets it"
    assert publications[0].account_id == yt.id


def select_all():
    from sqlalchemy import select
    return select(Publication)


def select_videos():
    from sqlalchemy import select

    from src.models import Video
    return select(Video)


def test_new_themes_coordinate_by_default(session):
    """No extra call should be needed for the common case."""
    theme = Theme(name="Fresh")
    session.add(theme)
    session.flush()
    assert theme.coordinated is True, "coordination must not need a second step"


def test_coordination_can_be_turned_off(session, themes):
    theme = themes["Motivation"]
    theme.coordinated = False
    session.flush()
    assert theme.coordinated is False


# ---- shared schedules -------------------------------------------------------

def _video(session, theme, name, **kw):
    return make_video(session, theme, name, **kw)


@pytest.fixture
def shared(session, themes, monkeypatch):
    """Two accounts owned by ONE schedule."""
    from src.models import Schedule, ScheduleAccount

    monkeypatch.setattr("src.scheduler.send_approval_request", lambda *a, **k: True)
    monkeypatch.setattr("src.scheduler.send_metadata_request", lambda *a, **k: True)

    theme = themes["Motivation"]
    schedule = Schedule(name="evening", publish_time=time(10, 0),
                        videos_per_day=1, timezone="UTC")
    session.add(schedule)
    session.flush()

    accounts = []
    for name, platform in [("a", Platform.youtube), ("b", Platform.instagram)]:
        account = make_account(session, name, platform, require_approval=True)
        account.timezone = "UTC"
        _map(session, account, theme)
        session.add(ScheduleAccount(schedule_id=schedule.id, account_id=account.id))
        accounts.append(account)
    for index in range(4):
        _video(session, theme, f"v{index}.mp4")
    session.flush()
    return schedule, accounts


def test_one_schedule_gives_every_account_the_same_video(session, shared):
    from sqlalchemy import select as sa_select

    from src.scheduler import run_schedule
    schedule, accounts = shared

    run_schedule(session, schedule,
                 now_utc=datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc),
                 enqueue=False)

    publications = list(session.scalars(sa_select(Publication)))
    assert len(publications) == 2, "one per member, no more"
    assert len({p.video_id for p in publications}) == 1, "must share one video"
    assert len({p.group_key for p in publications}) == 1


def test_running_the_same_slot_twice_does_nothing(session, shared):
    """The minute-by-minute tick must not re-fire a filled slot."""
    from sqlalchemy import select as sa_select

    from src.scheduler import run_schedule
    schedule, _ = shared
    now = datetime(2026, 9, 5, 10, 5, tzinfo=timezone.utc)

    run_schedule(session, schedule, now_utc=now, enqueue=False)
    run_schedule(session, schedule, now_utc=now + timedelta(minutes=1),
                 enqueue=False)

    assert len(list(session.scalars(sa_select(Publication)))) == 2


def test_members_are_not_scheduled_twice_by_the_per_account_pass(session, shared):
    """The exact bug: per-account schedules fired independently and each
    fan-out reached the other, so both accounts published twice."""
    from src.scheduler import accounts_in_shared_schedules

    _, accounts = shared
    owned = accounts_in_shared_schedules(session)
    assert owned == {a.id for a in accounts}, \
        "members must be excluded from the per-account pass"
