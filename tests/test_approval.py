"""The approval gate: nothing uploads without an explicit yes."""

from datetime import datetime, timezone

import pytest

from src.models import AccountTheme, Publication, PublicationStatus
from src.selection import propose_replacement, reserve_publication, select_next_video
from tests.conftest import make_account, make_video


def _map(session, account, theme, priority=10):
    session.add(AccountTheme(account_id=account.id, theme_id=theme.id,
                             priority=priority))
    session.flush()


def test_scheduled_post_waits_for_approval_by_default(session, themes):
    account = make_account(session, "guarded", require_approval=True)
    assert account.require_approval is True, "approval must be the default"


def test_rejection_offers_a_different_video(session, themes):
    account = make_account(session, "picky", require_approval=True)
    _map(session, account, themes["Motivation"])
    first = make_video(session, themes["Motivation"], "a.mp4")
    make_video(session, themes["Motivation"], "b.mp4")

    chosen, _ = select_next_video(session, account)
    publication = reserve_publication(session, account, chosen)
    publication.status = PublicationStatus.rejected
    session.flush()

    replacement, note = propose_replacement(session, publication, limit=5)
    assert replacement is not None, note
    assert replacement.video_id != first.id, "must offer a different video"
    assert replacement.proposal_attempt == 2


def test_rejected_video_is_never_offered_again(session, themes):
    account = make_account(session, "picky", require_approval=True)
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "a.mp4")
    make_video(session, themes["Motivation"], "b.mp4")

    chosen, _ = select_next_video(session, account)
    publication = reserve_publication(session, account, chosen)
    publication.status = PublicationStatus.rejected
    session.flush()

    replacement, _ = propose_replacement(session, publication, limit=5)
    third, reason = select_next_video(session, account)
    assert third is None, "both videos are now spoken for"


def test_slot_gives_up_after_five_rejections(session, themes):
    """The budget is per slot: the sixth proposal must not happen."""
    account = make_account(session, "very_picky", require_approval=True)
    _map(session, account, themes["Motivation"])
    for index in range(8):
        make_video(session, themes["Motivation"], f"v{index}.mp4")

    chosen, _ = select_next_video(session, account)
    current = reserve_publication(session, account, chosen)
    attempts = 1

    while True:
        current.status = PublicationStatus.rejected
        session.flush()
        replacement, note = propose_replacement(session, current, limit=5)
        if replacement is None:
            break
        current = replacement
        attempts += 1

    assert attempts == 5, f"expected 5 proposals, got {attempts}"
    assert "stopping for this slot" in note


def test_gives_up_when_library_runs_dry(session, themes):
    account = make_account(session, "small_library", require_approval=True)
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "only.mp4")

    chosen, _ = select_next_video(session, account)
    publication = reserve_publication(session, account, chosen)
    publication.status = PublicationStatus.rejected
    session.flush()

    replacement, note = propose_replacement(session, publication, limit=5)
    assert replacement is None
    assert "No alternative video" in note


def test_duplicate_publication_is_rejected_by_the_database(session, themes):
    """The unique constraint is the real duplicate guard, not application logic."""
    from sqlalchemy.exc import IntegrityError

    account = make_account(session, "dupe")
    _map(session, account, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "one.mp4")

    reserve_publication(session, account, video)
    with pytest.raises(IntegrityError):
        reserve_publication(session, account, video)
        session.flush()
