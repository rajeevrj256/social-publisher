"""The guided fill-in flow.

The wizard is stateless on purpose: each prompt embeds its own publication id
and field, so a bot restart mid-form loses nothing and the user never types an
id by hand.
"""

import pytest

from src.models import AccountTheme, PublicationStatus
from src.selection import reserve_publication, select_next_video
from src.telegram_bot import (
    FIELD_ORDER, MARKER, _prompt_text, metadata_keyboard, route_reply,
)
from tests.conftest import make_account, make_video


@pytest.fixture
def publication(session, themes):
    account = make_account(session, "wizard", require_approval=True)
    session.add(AccountTheme(account_id=account.id, theme_id=themes["Motivation"].id))
    session.flush()
    make_video(session, themes["Motivation"], "clip.mp4")
    video, _ = select_next_video(session, account)
    return reserve_publication(session, account, video), video


def test_prompt_carries_the_publication_id_and_field(session, publication):
    pub, video = publication
    text = _prompt_text(pub, "title", video)

    match = MARKER.search(text)
    assert match, "every prompt must embed its own id so replies can be routed"
    assert int(match.group(1)) == pub.id
    assert match.group(2) == "title"


def test_each_field_produces_a_distinct_marker(session, publication):
    pub, video = publication
    for field in FIELD_ORDER:
        match = MARKER.search(_prompt_text(pub, field, video))
        assert match.group(2) == field


def test_prompt_shows_the_existing_value_when_editing(session, publication):
    pub, video = publication
    pub.title = "Existing headline"
    assert "Existing headline" in _prompt_text(pub, "title", video)


def test_marker_survives_a_caption_containing_brackets(session, publication):
    """A user caption containing marker-like text must not hijack routing.

    Exercises the routing helper the handler actually calls, not the regex.
    """
    pub, video = publication
    pub.caption = "Random [#999 title] text"
    text = _prompt_text(pub, "caption", video)

    assert route_reply(text) == (pub.id, "caption")


def test_route_reply_ignores_unrelated_messages(session):
    assert route_reply("just a normal message") is None
    assert route_reply("") is None


def test_button_carries_the_id_so_the_user_never_types_it(session, publication):
    pub, _ = publication
    keyboard = metadata_keyboard(pub.id)
    payloads = [b["callback_data"] for row in keyboard["inline_keyboard"]
                for b in row]
    assert f"edit:{pub.id}:title" in payloads
    assert f"reject:{pub.id}" in payloads


def test_field_order_starts_with_title(session):
    assert FIELD_ORDER[0] == "title"
    assert set(FIELD_ORDER) == {"title", "caption", "hashtags"}


# ---- handler registration ---------------------------------------------------

def test_every_wizard_handler_is_actually_registered():
    """A handler that exists but is never added does nothing at all.

    The edit button silently did nothing in production because only the
    approval handler had been registered, so this asserts on the wiring rather
    than on the functions existing.
    """
    import inspect

    from src import telegram_bot

    source = inspect.getsource(telegram_bot.main)
    assert "on_approval_button" in source, "approve/reject buttons not wired"
    assert "on_edit_button" in source, "the 'Fill in details' button is not wired"
    assert "on_form_reply" in source, "wizard replies are not wired"
    assert "MessageHandler" in source, "no reply handler registered"


def test_edit_pattern_matches_the_button_payload():
    """The registered regex must accept what metadata_keyboard emits."""
    import inspect
    import re

    from src import telegram_bot

    source = inspect.getsource(telegram_bot.main)
    match = re.search(r'pattern=r"(\^edit:[^"]+)"', source)
    assert match, "no edit pattern registered"
    pattern = re.compile(match.group(1))

    payloads = [b["callback_data"]
                for row in telegram_bot.metadata_keyboard(7)["inline_keyboard"]
                for b in row]
    edit_payloads = [p for p in payloads if p.startswith("edit:")]
    assert edit_payloads, "keyboard emits no edit button"
    for payload in edit_payloads:
        assert pattern.match(payload), f"{payload} would never reach the handler"


# ---- callback timing --------------------------------------------------------

def test_callback_is_answered_before_the_work_starts():
    """Telegram invalidates a callback query after ~15s.

    Deciding can take minutes -- a rejection downloads the replacement video
    from Drive to build its preview -- so answering last raised
    "Query is too old and response timeout expired", left the button spinning
    and taught the operator to press again, which produced a second proposal.
    """
    import inspect

    from src import telegram_bot

    # The early-return guard for a malformed payload answers too, and matching
    # that one would let the real path regress unnoticed. Everything before the
    # guard's return is therefore discarded first.
    guard = "await query.answer()\n        return"
    for handler in (telegram_bot.on_approval_button,
                    telegram_bot.on_edit_button):
        source = inspect.getsource(handler)
        assert guard in source, f"{handler.__name__}: guard shape changed"
        body = source.split(guard, 1)[1]

        answers = [i for i in range(len(body))
                   if body.startswith("query.answer(", i)]
        assert answers, (
            f"{handler.__name__} never answers the callback on the real path")
        for slow in ("_decide(", "session_scope(", "_editable(", "to_thread("):
            if slow in body:
                assert answers[0] < body.index(slow), (
                    f"{handler.__name__} reaches {slow} before answering the "
                    f"callback; Telegram rejects the late answer")


def test_blocking_work_runs_off_the_event_loop():
    """_decide does network I/O. Run inline it freezes every other button."""
    import inspect

    from src import telegram_bot

    source = inspect.getsource(telegram_bot.on_approval_button)
    assert "asyncio.to_thread" in source, (
        "_decide blocks on Drive downloads and Telegram sends; running it in "
        "the event loop stops the bot answering anything else")


def test_an_error_handler_is_registered():
    """'No error handlers are registered' is how a dead button stays silent."""
    import inspect

    from src import telegram_bot

    assert "add_error_handler" in inspect.getsource(telegram_bot.main)
