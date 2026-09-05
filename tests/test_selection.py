"""The rules that decide what an account posts next."""

import random

from src.models import Platform, Publication, PublicationStatus, VideoAccountMapping
from src.selection import (
    eligible_themes, pending_count, pick_theme, reserve_publication,
    select_next_video,
)
from tests.conftest import make_account, make_video


def _map(session, account, theme, priority=10):
    from src.models import AccountTheme
    link = AccountTheme(account_id=account.id, theme_id=theme.id, priority=priority)
    session.add(link)
    session.flush()
    return link


def test_selects_video_from_mapped_theme(session, themes):
    account = make_account(session, "motivation_daily")
    _map(session, account, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "motivation001.mp4")
    make_video(session, themes["Gym"], "gym001.mp4")  # wrong theme, must be ignored

    chosen, reason = select_next_video(session, account)
    assert chosen is not None and chosen.id == video.id, reason


def test_account_without_themes_gets_nothing(session, themes):
    account = make_account(session, "empty")
    make_video(session, themes["Motivation"], "motivation001.mp4")
    chosen, reason = select_next_video(session, account)
    assert chosen is None
    assert "no themes" in reason


def test_same_video_serves_multiple_accounts(session, themes):
    """The core requirement: one file, many accounts, independent history."""
    first = make_account(session, "account_a")
    second = make_account(session, "account_b")
    _map(session, first, themes["Motivation"])
    _map(session, second, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "shared.mp4")

    chosen_a, _ = select_next_video(session, first)
    reserve_publication(session, first, chosen_a)

    chosen_b, reason = select_next_video(session, second)
    assert chosen_b is not None, reason
    assert chosen_b.id == video.id, "video must still be available to account B"


def test_video_not_offered_twice_to_same_account(session, themes):
    account = make_account(session, "account_a")
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "only.mp4")

    first, _ = select_next_video(session, account)
    reserve_publication(session, account, first)

    second, reason = select_next_video(session, account)
    assert second is None, "the duplicate rule must block a repeat"
    assert "no unpublished" in reason


def test_explicit_mapping_restricts_eligibility(session, themes):
    """Once a video has any mapping row, the mapping is authoritative."""
    allowed = make_account(session, "allowed")
    denied = make_account(session, "denied")
    _map(session, allowed, themes["Motivation"])
    _map(session, denied, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "exclusive.mp4")

    session.add(VideoAccountMapping(video_id=video.id, account_id=allowed.id))
    session.flush()

    chosen, _ = select_next_video(session, allowed)
    assert chosen is not None and chosen.id == video.id

    chosen, reason = select_next_video(session, denied)
    assert chosen is None, "unmapped account must not receive a mapped video"


def test_incompatible_video_is_skipped(session, themes):
    """A 3-hour file is not a Reel; it must be rejected before upload."""
    account = make_account(session, "ig", Platform.instagram)
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "long.mp4", duration=7200.0)

    chosen, reason = select_next_video(session, account)
    assert chosen is None
    assert "too long" in reason


def test_theme_weighting_follows_priority(session, themes):
    account = make_account(session, "weighted")
    high = _map(session, account, themes["Motivation"], priority=90)
    _map(session, account, themes["Success"], priority=10)

    mapped = eligible_themes(session, account)
    rng = random.Random(7)
    picks = [pick_theme(mapped, rng).theme_id for _ in range(400)]
    share = picks.count(high.theme_id) / len(picks)
    assert 0.8 < share < 0.98, f"expected ~90% high-priority, got {share:.2f}"


def test_pending_count_tracks_remaining_library(session, themes):
    account = make_account(session, "counter")
    _map(session, account, themes["Motivation"])
    for index in range(3):
        make_video(session, themes["Motivation"], f"v{index}.mp4")

    assert pending_count(session, account) == 3
    chosen, _ = select_next_video(session, account)
    reserve_publication(session, account, chosen)
    assert pending_count(session, account) == 2


def test_a_permanently_failed_video_is_not_offered_again(session, themes):
    """The unique constraint allows one row per (video, account, platform).

    Treating a failed_permanent row as "available again" made the selector
    re-offer the video and the insert then violated the constraint. Recovery is
    /retry on the existing row, never a second row.
    """
    from src.models import PublicationStatus

    account = make_account(session, "failer")
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "only.mp4")

    video, _ = select_next_video(session, account)
    publication = reserve_publication(session, account, video)
    publication.status = PublicationStatus.failed_permanent
    session.flush()

    again, reason = select_next_video(session, account)
    assert again is None, "a failed publication must not free the video"


def test_a_rejected_video_is_not_offered_again(session, themes):
    from src.models import PublicationStatus

    account = make_account(session, "rejecter")
    _map(session, account, themes["Motivation"])
    make_video(session, themes["Motivation"], "only.mp4")

    video, _ = select_next_video(session, account)
    publication = reserve_publication(session, account, video)
    publication.status = PublicationStatus.rejected
    session.flush()

    again, _ = select_next_video(session, account)
    assert again is None
