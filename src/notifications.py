"""Telegram outbound messages. Credentials are never included in any payload."""

from __future__ import annotations

import logging
from datetime import datetime

from contextlib import contextmanager

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


def send_message(text: str, chat_id: str | None = None,
                 keyboard: dict | None = None) -> bool:
    settings = get_settings()
    if not settings.telegram_bot_token:
        log.debug("telegram not configured; message dropped")
        return False
    target = chat_id or settings.telegram_chat_id
    if not target:
        return False
    try:
        payload = {"chat_id": target, "text": text,
                   "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = keyboard
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json=payload, timeout=20.0,
        )
        if response.status_code >= 400:
            log.error("telegram send failed: %s", response.status_code)
            return False
        return True
    except Exception as exc:
        log.error("telegram send error: %s", exc)
        return False


# Telegram caps a media caption at 1024 characters and a bot upload at 50 MB.
CAPTION_LIMIT = 1024
VIDEO_UPLOAD_LIMIT = 50 * 1024 * 1024


@contextmanager
def _preview_file(video):
    """A local path to preview, fetching from a remote source if needed.

    Yields None rather than raising: a preview that cannot be fetched must
    still send the text, because the approval itself matters more than the clip.
    """
    from .media import MediaUnavailable, materialize

    try:
        with materialize(video) as path:
            yield path
    except (MediaUnavailable, Exception) as exc:  # noqa: BLE001
        log.info("no preview for %s: %s", video.filename, exc)
        yield None


def send_video(file_path, caption: str, keyboard: dict | None = None) -> bool:
    """Send the actual clip so it can be judged before it is published.

    Falls back to a text message when the file is missing or too large for the
    Bot API - the approval must still reach the user either way.
    """
    from pathlib import Path

    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False

    path = Path(file_path)
    if not path.exists() or path.stat().st_size > VIDEO_UPLOAD_LIMIT:
        log.info("sending text instead of video for %s", path.name)
        return send_message(caption)

    head, tail = caption[:CAPTION_LIMIT], caption[CAPTION_LIMIT:]
    data = {"chat_id": settings.telegram_chat_id, "caption": head,
            "supports_streaming": "true"}
    if keyboard and not tail:
        # Buttons ride with the video only when nothing is left over; otherwise
        # they belong on the final message so they sit under the full text.
        import json as _json
        data["reply_markup"] = _json.dumps(keyboard)

    try:
        with path.open("rb") as handle:
            response = httpx.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendVideo",
                data=data, files={"video": (path.name, handle, "video/mp4")},
                timeout=180.0)
        if response.status_code >= 400:
            log.error("sendVideo failed (%s); falling back to text",
                      response.status_code)
            return send_message(caption)
    except Exception as exc:
        log.error("sendVideo error: %s; falling back to text", exc)
        return send_message(caption)

    if tail:
        return send_message(tail, keyboard=keyboard)
    return True


def publication_message(publication, account, video, theme_name: str) -> str:
    icon = "✅ Success" if publication.status.value == "published" else "❌ Failed"
    when = (publication.published_at or datetime.now()).strftime("%d %b %Y %H:%M")
    lines = [
        "🎬 PUBLISHED" if publication.status.value == "published" else "⚠️ FAILED",
        "",
        f"Account:\n@{account.username} ({account.platform.value})",
        "",
        f"Theme:\n{theme_name}",
        "",
        f"Video:\n{video.filename}",
    ]
    if publication.title:
        lines += ["", f"Title:\n{publication.title}"]
    if publication.platform_url:
        lines += ["", f"Link:\n{publication.platform_url}"]
    if publication.error_message and publication.status.value != "published":
        lines += ["", f"Error:\n{publication.error_message[:300]}"]
    lines += ["", f"Time:\n{when}", "", f"Status:\n{icon}"]
    return "\n".join(lines)


def approval_request_message(publication, account, video, theme_name: str) -> str:
    """Everything a human needs to judge the post before it exists publicly."""
    when = (publication.scheduled_at.strftime("%d %b %Y %H:%M")
            if publication.scheduled_at else "now")
    duration = f"{video.duration:.1f}s" if video.duration else "unknown"
    size = f"{video.width}x{video.height}" if video.width else "unknown"
    lines = [
        "🟡 APPROVAL NEEDED",
        "",
        f"Publication:\n#{publication.id}",
        "",
        f"Account:\n@{account.username} ({account.platform.value})",
        "",
        f"Theme:\n{theme_name}",
        "",
        f"Video:\n{video.filename}",
        f"({duration}, {size}, {video.codec or 'unknown codec'})",
        "",
        f"Title:\n{publication.title or '-'}",
        "",
        f"Caption:\n{publication.caption or '-'}",
        "",
        f"Hashtags:\n{publication.hashtags or '-'}",
        "",
        f"Scheduled:\n{when}",
        "",
        f"Approve:  /approve {publication.id}",
        f"Reject:   /reject {publication.id}   (permanent)",
        f"Not today: /defer {publication.id}   (back tomorrow)",
    ]
    return "\n".join(lines)


def approval_keyboard(publication_id: int) -> dict:
    """The three decisions. Reject is permanent; "Not today" frees the video
    again after midnight in the account's timezone.

    Kept as its own function so the registered callback pattern can be tested
    against the payloads actually emitted, rather than against a copy of them.
    """
    return {"inline_keyboard": [
        [
            {"text": "✅ Approve", "callback_data": f"approve:{publication_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{publication_id}"},
        ],
        # Second row: rejecting is permanent, skipping is not. Separating them
        # makes the irreversible one harder to hit by accident. Editing lives
        # here too, so wording can be changed without approving first.
        [
            {"text": "🕒 Not today", "callback_data": f"defer:{publication_id}"},
            {"text": "✍️ Edit details",
             "callback_data": f"edit:{publication_id}:title"},
        ],
    ]}


def send_approval_request(publication, account, video, theme_name: str) -> bool:
    """Inline buttons where available, with the text commands always shown as a
    fallback so approval never depends on the keyboard rendering."""
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.warning("telegram not configured - publication %s cannot be approved",
                    publication.id)
        return False
    text = approval_request_message(publication, account, video, theme_name)
    keyboard = approval_keyboard(publication.id)
    with _preview_file(video) as path:
        if path:
            return send_video(path, text, keyboard)
    return send_message(text, keyboard=keyboard)


def metadata_request_message(publication, account, video, theme_name: str,
                             missing: list[str]) -> str:
    """Ask for the wording rather than inventing it.

    Reached when no AI key is configured and the account has no caption
    template, so the only alternative would be posting a filename as a title.
    """
    duration = f"{video.duration:.1f}s" if video.duration else "unknown"
    size = f"{video.width}x{video.height}" if video.width else "unknown"
    return "\n".join([
        "✍️ DETAILS NEEDED",
        "",
        f"Publication:\n#{publication.id}",
        "",
        f"Account:\n@{account.username} ({account.platform.value})",
        "",
        f"Theme:\n{theme_name}",
        "",
        f"Video:\n{video.filename}",
        f"({duration}, {size})",
        "",
        f"Missing: {', '.join(missing)}",
        "",
        f"Suggested title:\n{publication.title or '-'}",
        f"Default hashtags:\n{publication.hashtags or '-'}",
        "",
        "Tap \"Fill in details\" below and I will ask for each field.",
        "",
        f"Or type it all at once:  /meta {publication.id}",
    ])


def metadata_keyboard(publication_id: int) -> dict:
    """The form has the same three outcomes as the approval card.

    "Skip this video" used to be wired to reject, so declining to write a
    caption burned the video permanently -- an answer to "not right now" that
    nobody means. Skipping is a deferral; rejecting is spelled out separately.
    """
    return {"inline_keyboard": [
        [{"text": "✍️ Fill in details",
          "callback_data": f"edit:{publication_id}:title"}],
        [
            {"text": "🕒 Not today", "callback_data": f"defer:{publication_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{publication_id}"},
        ],
    ]}


def send_metadata_request(publication, account, video, theme_name: str,
                          missing: list[str]) -> bool:
    text = metadata_request_message(publication, account, video, theme_name,
                                    missing)
    keyboard = metadata_keyboard(publication.id)
    with _preview_file(video) as path:
        if path:
            return send_video(path, text, keyboard)
    return send_message(text, keyboard=keyboard)
