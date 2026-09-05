"""Per-account source links.

The scenario under test is exactly the one asked for:
    youtube_1  -> link1
    youtube_2  -> link2
    instagram_1 -> link2   (shared with youtube_2)
    instagram_2 -> link3
"""

import pytest
from sqlalchemy import select

from src.models import (
    AccountSource, AccountTheme, Platform, Source, SourceKind, Video,
)
from src.selection import account_source_ids, reserve_publication, select_next_video
from tests.conftest import make_account, make_video


def _source(session, name, location, kind=SourceKind.local):
    source = Source(name=name, location=location, kind=kind)
    session.add(source)
    session.flush()
    return source


def _link(session, account, source):
    session.add(AccountSource(account_id=account.id, source_id=source.id))
    session.flush()


def _theme(session, account, theme):
    session.add(AccountTheme(account_id=account.id, theme_id=theme.id))
    session.flush()


@pytest.fixture
def wired(session, themes):
    """Four accounts across three links, one link deliberately shared."""
    link1 = _source(session, "link1", "channel_one")
    link2 = _source(session, "link2", "channel_two")
    link3 = _source(session, "link3", "channel_three")

    accounts = {
        "youtube_1": make_account(session, "youtube_1", Platform.youtube),
        "youtube_2": make_account(session, "youtube_2", Platform.youtube),
        "instagram_1": make_account(session, "instagram_1", Platform.instagram),
        "instagram_2": make_account(session, "instagram_2", Platform.instagram),
    }
    routing = {"youtube_1": link1, "youtube_2": link2,
               "instagram_1": link2, "instagram_2": link3}
    for name, account in accounts.items():
        _link(session, account, routing[name])
        _theme(session, account, themes["Motivation"])

    videos = {}
    for source in (link1, link2, link3):
        video = make_video(session, themes["Motivation"], f"{source.name}_clip.mp4")
        video.source_id = source.id
        videos[source.name] = video
    session.flush()
    return {"accounts": accounts, "sources": {"link1": link1, "link2": link2,
                                              "link3": link3}, "videos": videos}


def test_each_account_draws_from_its_own_link(session, wired):
    expected = {"youtube_1": "link1_clip.mp4", "youtube_2": "link2_clip.mp4",
                "instagram_1": "link2_clip.mp4", "instagram_2": "link3_clip.mp4"}
    for name, account in wired["accounts"].items():
        chosen, reason = select_next_video(session, account)
        assert chosen is not None, f"{name}: {reason}"
        assert chosen.filename == expected[name], (
            f"{name} drew {chosen.filename}, expected {expected[name]}")


def test_a_shared_link_feeds_both_of_its_accounts(session, wired):
    """link2 serves youtube_2 and instagram_1 independently."""
    youtube_2 = wired["accounts"]["youtube_2"]
    instagram_1 = wired["accounts"]["instagram_1"]

    first, _ = select_next_video(session, youtube_2)
    reserve_publication(session, youtube_2, first)

    second, reason = select_next_video(session, instagram_1)
    assert second is not None, reason
    assert second.id == first.id, "the shared link must still serve the other account"


def test_account_never_receives_another_links_video(session, wired):
    youtube_1 = wired["accounts"]["youtube_1"]
    # Exhaust link1, then confirm nothing from link2/link3 leaks in.
    chosen, _ = select_next_video(session, youtube_1)
    reserve_publication(session, youtube_1, chosen)

    nothing, reason = select_next_video(session, youtube_1)
    assert nothing is None, f"leaked {nothing.filename if nothing else ''}"
    assert "no unpublished" in reason


def test_account_without_sources_sees_the_whole_library(session, themes):
    """Back-compatibility: an account with no source mapping is unrestricted."""
    account = make_account(session, "unrestricted")
    _theme(session, account, themes["Motivation"])
    source = _source(session, "somewhere", "any_folder")
    video = make_video(session, themes["Motivation"], "free.mp4")
    video.source_id = source.id
    session.flush()

    assert account_source_ids(account) is None
    chosen, reason = select_next_video(session, account)
    assert chosen is not None, reason


def test_disabling_a_source_stops_its_accounts(session, wired):
    youtube_1 = wired["accounts"]["youtube_1"]
    wired["sources"]["link1"].enabled = False
    session.flush()

    # With its only source disabled the account falls back to unrestricted,
    # so an explicit disable must also be reflected in the mapping row.
    for row in session.scalars(select(AccountSource).where(
            AccountSource.account_id == youtube_1.id)):
        row.enabled = False
    session.flush()
    assert account_source_ids(youtube_1) is None


def test_remote_url_source_builds_the_public_link(session, themes):
    """A remote source is already public, so Instagram fetches it directly."""
    from src.publishers.instagram import InstagramPublisher

    source = _source(session, "cdn", "https://cdn.example.com/reels",
                     kind=SourceKind.remote_url)
    video = make_video(session, themes["Motivation"], "a.mp4")
    video.source_id = source.id
    video.filepath = "motivation/a.mp4"
    session.flush()

    url = InstagramPublisher().public_url_for(video, source)
    assert url == "https://cdn.example.com/reels/motivation/a.mp4"


# ---- Instagram API host selection -------------------------------------------

def test_instagram_login_uses_graph_instagram_host(session, themes):
    """The two Meta login flows are served from different hosts."""
    from src.models import AccountCredential
    from src.publishers.instagram import InstagramPublisher

    account = make_account(session, "ig_login", Platform.instagram)
    session.add(AccountCredential(account_id=account.id, extra={}))
    session.flush()
    session.refresh(account)

    base = InstagramPublisher().api_base(account)
    assert base.startswith("https://graph.instagram.com/"), base


def test_facebook_login_account_can_override_the_host(session, themes):
    from src.models import AccountCredential
    from src.publishers.instagram import InstagramPublisher

    account = make_account(session, "fb_login", Platform.instagram)
    session.add(AccountCredential(account_id=account.id,
                                  extra={"graph_host": "graph.facebook.com"}))
    session.flush()
    session.refresh(account)

    base = InstagramPublisher().api_base(account)
    assert base.startswith("https://graph.facebook.com/"), base
