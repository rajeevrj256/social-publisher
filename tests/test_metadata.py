"""Metadata without an AI key: the bot must ask, never invent."""

import pytest

from src.metadata_ai import generate, metadata_complete
from src.models import AccountAIConfig, AccountTheme, HashtagSet, Publication
from src.selection import reserve_publication, select_next_video
from src.telegram_bot import _parse_meta_block
from tests.conftest import make_account, make_video


def _map(session, account, theme):
    session.add(AccountTheme(account_id=account.id, theme_id=theme.id))
    session.flush()


def test_without_ai_or_template_nothing_is_authored(session, themes):
    """The critical case: no key, no template. A filename is not a caption."""
    account = make_account(session, "plain")
    video = make_video(session, themes["Motivation"], "motivation001.mp4")

    meta = generate(session, account, video)
    assert meta.authored is False, "a filename fallback must not count as authored"
    assert meta.caption == ""


def test_a_caption_template_counts_as_authored(session, themes):
    account = make_account(session, "templated")
    session.add(AccountAIConfig(account_id=account.id, ai_enabled=False,
                                caption_template="Daily {theme}. Keep going."))
    session.flush()
    session.refresh(account)
    video = make_video(session, themes["Motivation"], "m1.mp4")

    meta = generate(session, account, video)
    assert meta.authored is True
    assert meta.caption == "Daily Motivation. Keep going."


def test_default_hashtags_are_used(session, themes):
    account = make_account(session, "tagged")
    session.add(HashtagSet(account_id=account.id, hashtags="#motivation #success"))
    session.flush()
    video = make_video(session, themes["Motivation"], "m1.mp4")

    assert generate(session, account, video).hashtags == "#motivation #success"


def test_theme_hashtags_beat_the_account_default(session, themes):
    account = make_account(session, "tagged")
    session.add(HashtagSet(account_id=account.id, hashtags="#default"))
    session.add(HashtagSet(account_id=account.id,
                           theme_id=themes["Motivation"].id, hashtags="#specific"))
    session.flush()
    video = make_video(session, themes["Motivation"], "m1.mp4")

    assert generate(session, account, video).hashtags == "#specific"


def test_missing_fields_are_reported(session, themes):
    account = make_account(session, "a")
    _map(session, account, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "m1.mp4")
    publication = reserve_publication(session, account, video)

    assert metadata_complete(publication) == ["title", "caption"]
    publication.title = "A title"
    assert metadata_complete(publication) == ["caption"]
    publication.caption = "A caption"
    assert metadata_complete(publication) == []


def test_whitespace_is_not_a_caption(session, themes):
    account = make_account(session, "a")
    _map(session, account, themes["Motivation"])
    video = make_video(session, themes["Motivation"], "m1.mp4")
    publication = reserve_publication(session, account, video)
    publication.title, publication.caption = "  ", "\n\t "

    assert metadata_complete(publication) == ["title", "caption"]


# ---- the /meta parser -------------------------------------------------------

def test_meta_block_parses_all_three_fields():
    parsed = _parse_meta_block(
        "/meta 12\nTitle: Never Give Up\nCaption: Keep moving.\nTags: #a #b")
    assert parsed == {"title": "Never Give Up", "caption": "Keep moving.",
                      "hashtags": "#a #b"}


def test_meta_block_is_case_insensitive_and_accepts_aliases():
    parsed = _parse_meta_block("/meta 1\ntitle: A\nDESCRIPTION: B\nhashtags: #c")
    assert parsed == {"title": "A", "caption": "B", "hashtags": "#c"}


def test_caption_may_contain_colons_and_span_lines():
    """Real captions have colons and line breaks; the parser must not mangle them."""
    parsed = _parse_meta_block(
        "/meta 3\nTitle: Rule 1: never quit\nCaption: Line one\nstill line one")
    assert parsed["title"] == "Rule 1: never quit"
    assert parsed["caption"] == "Line one\nstill line one"


def test_partial_meta_block_is_accepted():
    assert _parse_meta_block("/meta 4\nCaption: only this") == {"caption": "only this"}


def test_empty_block_yields_nothing():
    assert _parse_meta_block("/meta 5") == {}
