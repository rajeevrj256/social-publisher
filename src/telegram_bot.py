"""Telegram control surface. This is the only interface - there is no dashboard.

Every handler is gated on the configured chat id: an unknown sender is ignored
silently rather than answered, so the bot does not confirm its own existence to
someone probing it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from telegram import ForceReply, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .config import get_settings
from .db import session_scope
from .models import (
    Account, AccountTheme, Platform, Publication, PublicationStatus, Theme, Video,
)
from .jobs import prepare_metadata, request_decision
from .metadata_ai import metadata_complete
from .notifications import (
    metadata_keyboard, send_approval_request, send_message,
)
from .scheduler import due_slots, schedule_account
from .selection import (
    next_local_midnight, pending_count, propose_replacement,
    reserve_publication, select_next_video,
)

log = logging.getLogger(__name__)


def restricted(handler):
    """Only the configured chat may command the bot."""
    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        allowed = str(get_settings().telegram_chat_id).strip()
        sender = str(update.effective_chat.id) if update.effective_chat else ""
        if not allowed or sender != allowed:
            log.warning("ignored command from unauthorised chat %s", sender)
            return
        return await handler(update, context)
    return wrapper


def _find_account(session, token: str) -> Account | None:
    token = token.lstrip("@").strip().lower()
    return session.scalar(
        select(Account).where(
            func.lower(Account.username) == token)
    ) or session.scalar(
        select(Account).where(func.lower(Account.account_name) == token))


@restricted
async def cmd_status(update: Update, _ctx) -> None:
    lines = ["📊 ACCOUNTS", ""]
    with session_scope() as session:
        accounts = list(session.scalars(select(Account).order_by(Account.id)))
        if not accounts:
            lines.append("No accounts configured yet.")
        for account in accounts:
            state = "🟢 Active" if account.enabled else "🔴 Paused"
            slots = due_slots(session, account) if account.enabled else []
            pending = pending_count(session, account)
            lines += [f"@{account.username} ({account.platform.value})", state]
            if slots:
                lines.append(f"Due now: {len(slots)} slot(s)")
            lines.append(f"Pending: {pending:,}")
            lines.append("")
    await update.message.reply_text("\n".join(lines))


@restricted
async def cmd_accounts(update: Update, _ctx) -> None:
    lines = ["🔗 CONNECTED ACCOUNTS", ""]
    with session_scope() as session:
        for account in session.scalars(select(Account).order_by(Account.platform)):
            has_token = bool(account.credentials
                             and account.credentials.access_token_encrypted)
            lines.append(
                f"{account.platform.value}: @{account.username} "
                f"{'🔑' if has_token else '⚠️ no token'}"
                f"{'' if account.enabled else ' (paused)'}")
    await update.message.reply_text("\n".join(lines) or "none")


@restricted
async def cmd_queue(update: Update, _ctx) -> None:
    lines = ["🗓 UPCOMING", ""]
    with session_scope() as session:
        rows = list(session.scalars(
            select(Publication).where(
                Publication.status.in_([PublicationStatus.pending,
                                        PublicationStatus.queued,
                                        PublicationStatus.publishing])
            ).order_by(Publication.scheduled_at).limit(20)
        ))
        for publication in rows:
            account = session.get(Account, publication.account_id)
            video = session.get(Video, publication.video_id)
            when = (publication.scheduled_at.strftime("%d %b %H:%M")
                    if publication.scheduled_at else "asap")
            lines.append(f"{when} @{account.username} - {video.filename} "
                         f"[{publication.status.value}]")
    await update.message.reply_text("\n".join(lines) if len(lines) > 2
                                    else "Queue is empty.")


@restricted
async def cmd_recent(update: Update, _ctx) -> None:
    lines = ["🕒 RECENT", ""]
    with session_scope() as session:
        rows = list(session.scalars(
            select(Publication)
            .where(Publication.status == PublicationStatus.published)
            .order_by(Publication.published_at.desc()).limit(15)
        ))
        for publication in rows:
            account = session.get(Account, publication.account_id)
            video = session.get(Video, publication.video_id)
            when = (publication.published_at.strftime("%d %b %H:%M")
                    if publication.published_at else "-")
            lines.append(f"{when} @{account.username} - {video.filename}")
            if publication.platform_url:
                lines.append(f"   {publication.platform_url}")
    await update.message.reply_text("\n".join(lines) if len(lines) > 2
                                    else "Nothing published yet.")


@restricted
async def cmd_failed(update: Update, _ctx) -> None:
    lines = ["❌ FAILED / NEEDS REVIEW", ""]
    with session_scope() as session:
        rows = list(session.scalars(
            select(Publication).where(
                Publication.status.in_([PublicationStatus.failed,
                                        PublicationStatus.failed_permanent,
                                        PublicationStatus.needs_review])
            ).order_by(Publication.updated_at.desc()).limit(15)
        ))
        for publication in rows:
            account = session.get(Account, publication.account_id)
            video = session.get(Video, publication.video_id)
            lines.append(
                f"#{publication.id} @{account.username} - {video.filename} "
                f"[{publication.status.value}, tries {publication.retry_count}]")
            if publication.error_message:
                lines.append(f"   {publication.error_message[:160]}")
    await update.message.reply_text("\n".join(lines) if len(lines) > 2
                                    else "No failures. 🎉")


@restricted
async def cmd_retry(update: Update, ctx) -> None:
    from .worker import get_queue

    args = ctx.args or []
    with session_scope() as session:
        if args and args[0].isdigit():
            targets = [session.get(Publication, int(args[0]))]
            targets = [t for t in targets if t]
        else:
            targets = list(session.scalars(
                select(Publication).where(
                    Publication.status.in_([PublicationStatus.failed,
                                            PublicationStatus.failed_permanent])
                ).limit(20)))
        if not targets:
            await update.message.reply_text("Nothing to retry.")
            return
        ids = []
        for publication in targets:
            # Reset the counter: an explicit human retry is a fresh decision,
            # not a continuation of the automatic attempts.
            publication.status = PublicationStatus.queued
            publication.retry_count = 0
            publication.error_message = None
            ids.append(publication.id)

    queue = get_queue()
    for publication_id in ids:
        queue.enqueue("src.jobs.publish_publication", publication_id,
                      job_timeout=3600)
    await update.message.reply_text(f"Re-queued {len(ids)} publication(s).")


@restricted
async def cmd_pause(update: Update, ctx) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /pause <account>")
        return
    with session_scope() as session:
        account = _find_account(session, ctx.args[0])
        if not account:
            await update.message.reply_text("Account not found.")
            return
        account.enabled = False
        name = account.username
    await update.message.reply_text(f"⏸ Paused @{name}")


@restricted
async def cmd_resume(update: Update, ctx) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /resume <account>")
        return
    with session_scope() as session:
        account = _find_account(session, ctx.args[0])
        if not account:
            await update.message.reply_text("Account not found.")
            return
        account.enabled = True
        name = account.username
    await update.message.reply_text(f"▶️ Resumed @{name}")


@restricted
async def cmd_publish(update: Update, ctx) -> None:
    """Publish the next eligible video for one account, right now."""
    from .worker import get_queue

    if not ctx.args:
        await update.message.reply_text("Usage: /publish <account>")
        return
    with session_scope() as session:
        account = _find_account(session, ctx.args[0])
        if not account:
            await update.message.reply_text("Account not found.")
            return
        video, reason = select_next_video(session, account)
        if video is None:
            await update.message.reply_text(f"Nothing to publish: {reason}")
            return
        publication = reserve_publication(session, account, video)
        publication.status = PublicationStatus.queued
        session.flush()
        publication_id, filename, username = (
            publication.id, video.filename, account.username)

    get_queue().enqueue("src.jobs.publish_publication", publication_id,
                        job_timeout=3600)
    await update.message.reply_text(
        f"🚀 Queued {filename} for @{username} (publication #{publication_id})")


@restricted
async def cmd_theme(update: Update, _ctx) -> None:
    lines = ["🎯 ACCOUNT → THEMES", ""]
    with session_scope() as session:
        for account in session.scalars(select(Account).order_by(Account.id)):
            mappings = list(session.scalars(
                select(AccountTheme).where(AccountTheme.account_id == account.id)))
            lines.append(f"@{account.username}")
            if not mappings:
                lines.append("   (no themes mapped)")
            for mapping in mappings:
                theme = session.get(Theme, mapping.theme_id)
                available = session.scalar(
                    select(func.count()).select_from(Video).where(
                        Video.theme_id == theme.id, Video.active.is_(True)))
                lines.append(f"   {theme.name} (priority {mapping.priority}, "
                             f"{available} videos)"
                             f"{'' if mapping.enabled else ' [off]'}")
            lines.append("")
    await update.message.reply_text("\n".join(lines))




def _decide(session, publication_id: int, decision: str, actor: str) -> str:
    """Apply an approval decision: "approve", "reject" or "defer".

    Idempotent: a double tap on the button must not queue the same upload twice.
    """
    publication = session.get(Publication, publication_id)
    if publication is None:
        return f"Publication #{publication_id} not found."
    if publication.status == PublicationStatus.published:
        return f"#{publication_id} is already published."
    if publication.status not in (PublicationStatus.awaiting_approval,
                                  PublicationStatus.awaiting_metadata,
                                  PublicationStatus.rejected,
                                  PublicationStatus.deferred,
                                  PublicationStatus.approved):
        return (f"#{publication_id} is {publication.status.value}, "
                f"not waiting for approval.")

    # A rejected or deferred row may still be approved -- changing your mind is
    # allowed -- but it must not be rejected or deferred *again*: each pass
    # proposes a replacement, so a double tap produced two new videos for one
    # slot. Telegram also redelivers a callback when the first answer is slow.
    if decision in ("reject", "defer") and publication.status in (
            PublicationStatus.rejected, PublicationStatus.deferred):
        return (f"#{publication_id} is already {publication.status.value}; "
                f"a replacement was already offered.")

    account = session.get(Account, publication.account_id)
    video = session.get(Video, publication.video_id)
    if decision == "approve":
        if publication.status == PublicationStatus.approved:
            return f"#{publication_id} was already approved."
        missing = metadata_complete(publication)
        if missing:
            return (f"#{publication_id} still needs {', '.join(missing)}. "
                    f"Send /meta {publication_id} first.")
        publication.status = PublicationStatus.queued
        publication.available_after = None   # approving overrides a deferral
        publication.approved_at = datetime.now(timezone.utc)
        publication.approved_by = actor
        session.flush()
        return (f"✅ Approved #{publication_id}: {video.filename} "
                f"-> @{account.username}. Uploading now.")

    if decision == "defer":
        # Not a verdict on the video, only on today. The row stays (the unique
        # constraint allows just one per video+account+platform) and keeps
        # blocking until available_after passes, at which point selection lets
        # the video through again and reserve_publication reuses this row.
        publication.status = PublicationStatus.deferred
        publication.available_after = next_local_midnight(account)
        publication.approved_by = actor
        publication.error_message = "deferred on Telegram - back tomorrow"
        session.flush()
        back = publication.available_after.astimezone(
            ZoneInfo(account.timezone or get_settings().tz))
        lines = [f"🕒 Not today #{publication_id}: {video.filename} "
                 f"-> @{account.username}.",
                 f"Back in the pool from {back:%d %b %H:%M} "
                 f"({account.timezone}).",
                 f"Changed your mind? /approve {publication_id} still works, "
                 f"or /meta {publication_id} to write the details."]
    else:
        publication.status = PublicationStatus.rejected
        publication.approved_by = actor
        publication.error_message = "rejected on Telegram"
        session.flush()

        lines = [f"❌ Rejected #{publication_id}: {video.filename} "
                 f"-> @{account.username}."]
    replacement, note = propose_replacement(session, publication)
    lines.append(note)
    if replacement is not None:
        next_video = session.get(Video, replacement.video_id)
        # Shared with the scheduler: a replacement with no caption must arrive
        # as a form to fill in, not as an approval card with nothing to approve.
        asked = request_decision(session, replacement, account, next_video)
        lines.append(
            f"Sent a different video: {next_video.filename}"
            + (" - it needs details first." if asked == "metadata" else ""))
    return "\n".join(lines)


@restricted
async def cmd_approve(update: Update, ctx) -> None:
    from .worker import get_queue

    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /approve <publication id>")
        return
    publication_id = int(ctx.args[0].lstrip("#"))
    actor = str(update.effective_user.id) if update.effective_user else "telegram"
    with session_scope() as session:
        message = _decide(session, publication_id, "approve", actor)
        queued = session.get(Publication, publication_id)
        should_enqueue = queued is not None and queued.status == PublicationStatus.queued
    if should_enqueue:
        get_queue().enqueue("src.jobs.publish_publication", publication_id,
                            job_timeout=3600, result_ttl=86400)
    await update.message.reply_text(message)


@restricted
async def cmd_reject(update: Update, ctx) -> None:
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /reject <publication id>")
        return
    actor = str(update.effective_user.id) if update.effective_user else "telegram"
    with session_scope() as session:
        message = _decide(session, int(ctx.args[0].lstrip("#")),
                          "reject", actor)
    await update.message.reply_text(message)


@restricted
async def cmd_defer(update: Update, ctx) -> None:
    """Skip a video for today only. It returns to the pool tomorrow."""
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /defer <publication id>")
        return
    actor = str(update.effective_user.id) if update.effective_user else "telegram"
    with session_scope() as session:
        message = _decide(session, int(ctx.args[0].lstrip("#")),
                          "defer", actor)
    await update.message.reply_text(message)


@restricted
async def cmd_pending(update: Update, _ctx) -> None:
    lines = ["🟡 AWAITING YOUR APPROVAL", ""]
    with session_scope() as session:
        rows = list(session.scalars(
            select(Publication)
            .where(Publication.status == PublicationStatus.awaiting_approval)
            .order_by(Publication.scheduled_at).limit(20)))
        for publication in rows:
            account = session.get(Account, publication.account_id)
            video = session.get(Video, publication.video_id)
            lines.append(f"#{publication.id} @{account.username} - {video.filename}")
            lines.append(f"   {publication.title or '-'}")
            lines.append(f"   /approve {publication.id}   /reject {publication.id}")
    await update.message.reply_text("\n".join(lines) if len(lines) > 2
                                    else "Nothing waiting for approval.")


async def on_approval_button(update: Update, _ctx) -> None:
    """Inline button handler. Access is re-checked here: a button can be
    forwarded, so the callback must not trust that it came from the original chat."""
    from .worker import get_queue

    query = update.callback_query
    if query is None:
        return
    allowed = str(get_settings().telegram_chat_id).strip()
    sender = str(query.message.chat.id) if query.message else ""
    if not allowed or sender != allowed:
        await query.answer("Not authorised.", show_alert=True)
        return

    action, _, raw_id = (query.data or "").partition(":")
    if not raw_id.isdigit():
        await query.answer()
        return
    publication_id = int(raw_id)
    actor = str(query.from_user.id) if query.from_user else "telegram"

    # Answer BEFORE doing the work. Telegram invalidates a callback query after
    # about 15 seconds, and deciding can take minutes: a rejection proposes a
    # replacement, which downloads that video from Drive to build the preview.
    # Answering last raised "Query is too old", left the button spinning, and
    # taught the operator to press again.
    await query.answer("Working...")
    try:
        # Drop the buttons immediately so the decision cannot be re-pressed
        # while the replacement is still being fetched.
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    def work() -> tuple[str, bool]:
        with session_scope() as session:
            text = _decide(session, publication_id, action, actor)
            publication = session.get(Publication, publication_id)
            queued = (publication is not None
                      and publication.status == PublicationStatus.queued)
        return text, queued

    # _decide is synchronous and does network I/O (Drive downloads, Telegram
    # sends). Run inline it would block the whole bot event loop, so every
    # other button -- "Fill in details" included -- stops responding until it
    # finishes.
    message, should_enqueue = await asyncio.to_thread(work)

    if should_enqueue:
        get_queue().enqueue("src.jobs.publish_publication", publication_id,
                            job_timeout=3600, result_ttl=86400)
    if message:
        await query.message.reply_text(message)




def _editable(session, publication_id: int):
    """Fetch a publication whose wording may still be changed."""
    publication = session.get(Publication, publication_id)
    if publication is None:
        return None, f"Publication #{publication_id} not found."
    if publication.status in (PublicationStatus.published,
                              PublicationStatus.publishing):
        return None, (f"#{publication_id} is already {publication.status.value}; "
                      f"its metadata is part of the record and cannot change.")
    return publication, None


def _after_metadata_change(session, publication) -> str:
    """Promote to approval once the wording is complete, and show the card."""
    missing = metadata_complete(publication)
    account = session.get(Account, publication.account_id)
    video = session.get(Video, publication.video_id)
    if missing:
        publication.status = PublicationStatus.awaiting_metadata
        return f"Saved. Still missing: {', '.join(missing)}."

    # Writing the wording for a deferred post is itself a change of mind, so
    # the deferral is lifted rather than leaving a ready post still blocked.
    publication.status = PublicationStatus.awaiting_approval
    publication.available_after = None
    session.flush()
    send_approval_request(publication, account, video,
                          video.theme.name if video.theme else "unknown")
    return (f"Saved. #{publication.id} is ready - approve it with "
            f"/approve {publication.id}")


def _parse_meta_block(text: str) -> dict:
    """Parse the multi-line /meta form.

    Field names are matched case-insensitively and everything after the colon is
    taken verbatim, so captions may contain colons and emoji.
    """
    fields, current = {}, None
    aliases = {"title": "title", "caption": "caption", "description": "caption",
               "tags": "hashtags", "hashtags": "hashtags"}
    for raw_line in text.splitlines()[1:]:      # line 0 is the command itself
        line = raw_line.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        candidate = aliases.get(key.strip().lower()) if sep else None
        if candidate:
            current = candidate
            fields[current] = value.strip()
        elif current:
            fields[current] += "\n" + line     # continuation of a long caption
    return {k: v for k, v in fields.items() if v}


@restricted
async def cmd_meta(update: Update, ctx) -> None:
    """/meta <id> followed by Title:/Caption:/Tags: lines."""
    text = update.message.text or ""
    if not ctx.args or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text(
            "Usage:\n/meta <id>\nTitle: ...\nCaption: ...\nTags: #a #b")
        return
    fields = _parse_meta_block(text)
    if not fields:
        await update.message.reply_text(
            "No fields found. Put Title:, Caption: and Tags: on their own lines "
            "under the command.")
        return

    with session_scope() as session:
        publication, error = _editable(session, int(ctx.args[0].lstrip("#")))
        if error:
            await update.message.reply_text(error)
            return
        for key, value in fields.items():
            setattr(publication, key, value)
        if "caption" in fields:
            publication.description = fields["caption"]
        session.flush()
        message = _after_metadata_change(session, publication)
    await update.message.reply_text(f"✍️ {', '.join(fields)} updated.\n{message}")


async def _set_single_field(update, ctx, field: str, label: str) -> None:
    if len(ctx.args) < 2 or not ctx.args[0].lstrip("#").isdigit():
        await update.message.reply_text(f"Usage: /{label} <id> <text>")
        return
    publication_id = int(ctx.args[0].lstrip("#"))
    # Take the raw text so punctuation and spacing survive intact.
    value = (update.message.text or "").split(None, 2)
    value = value[2].strip() if len(value) > 2 else ""
    if not value:
        await update.message.reply_text(f"Usage: /{label} <id> <text>")
        return

    with session_scope() as session:
        publication, error = _editable(session, publication_id)
        if error:
            await update.message.reply_text(error)
            return
        setattr(publication, field, value)
        if field == "caption":
            publication.description = value
        session.flush()
        message = _after_metadata_change(session, publication)
    await update.message.reply_text(f"✍️ {label} updated.\n{message}")


@restricted
async def cmd_title(update: Update, ctx) -> None:
    await _set_single_field(update, ctx, "title", "title")


@restricted
async def cmd_caption(update: Update, ctx) -> None:
    await _set_single_field(update, ctx, "caption", "caption")


@restricted
async def cmd_tags(update: Update, ctx) -> None:
    await _set_single_field(update, ctx, "hashtags", "tags")


@restricted
async def cmd_needs(update: Update, _ctx) -> None:
    lines = ["✍️ WAITING ON YOUR WORDING", ""]
    with session_scope() as session:
        rows = list(session.scalars(
            select(Publication)
            .where(Publication.status == PublicationStatus.awaiting_metadata)
            .order_by(Publication.scheduled_at).limit(20)))
        for publication in rows:
            account = session.get(Account, publication.account_id)
            video = session.get(Video, publication.video_id)
            missing = metadata_complete(publication)
            lines.append(f"#{publication.id} @{account.username} - {video.filename}")
            lines.append(f"   missing: {', '.join(missing) or 'nothing'}")
            lines.append(f"   /meta {publication.id}")
    await update.message.reply_text("\n".join(lines) if len(lines) > 2
                                    else "Nothing waiting on wording.")




# The guided fill-in flow. Each prompt carries its own publication id and field
# in a marker, so the wizard is stateless: a bot restart mid-form loses nothing,
# and the user never types an id.
FIELD_ORDER = ["title", "caption", "hashtags"]
FIELD_LABEL = {"title": "Title", "caption": "Caption", "hashtags": "Hashtags"}
FIELD_HINT = {
    "title": "e.g. Never Give Up          (max 100 characters)",
    "caption": "e.g. Keep moving forward.",
    "hashtags": "e.g. #motivation #success     (send - to leave empty)",
}
MARKER = re.compile(r"\[#(\d+) (title|caption|hashtags)\]")


def route_reply(prompt_text: str) -> tuple[int, str] | None:
    """Which publication and field a reply belongs to, or None.

    Always the LAST marker: the prompt echoes the current value back to the
    user, and that text could itself contain marker-like characters.
    """
    matches = MARKER.findall(prompt_text or "")
    if not matches:
        return None
    return int(matches[-1][0]), matches[-1][1]


def _prompt_text(publication, field: str, video) -> str:
    step = FIELD_ORDER.index(field) + 1
    current = getattr(publication, field, None)
    lines = [
        f"✍️ Step {step} of {len(FIELD_ORDER)} · {FIELD_LABEL[field]}",
        "",
        f"Video: {video.filename}",
    ]
    if current:
        lines += ["", f"Current: {current}"]
    lines += ["", FIELD_HINT[field], "", f"[#{publication.id} {field}]"]
    return "\n".join(lines)


async def _ask_for(message, publication, field: str, video) -> None:
    """Open the reply box pre-focused on one field."""
    await message.reply_text(
        _prompt_text(publication, field, video),
        # selective=True targets the sender of the message being replied to --
        # which here is the bot itself, so Telegram opened the reply box for
        # nobody and the prompt appeared with no way to answer it. This is a
        # private chat with one authorised user, so target everyone.
        reply_markup=ForceReply(selective=False,
                                input_field_placeholder=FIELD_LABEL[field]),
    )


async def on_edit_button(update: Update, _ctx) -> None:
    query = update.callback_query
    if query is None:
        return
    allowed = str(get_settings().telegram_chat_id).strip()
    sender = str(query.message.chat.id) if query.message else ""
    if not allowed or sender != allowed:
        await query.answer("Not authorised.", show_alert=True)
        return

    _, _, rest = (query.data or "").partition(":")
    raw_id, _, field = rest.partition(":")
    if not raw_id.isdigit() or field not in FIELD_ORDER:
        await query.answer()
        return

    # Answered first for the same reason as the decision buttons: the callback
    # expires in about 15 seconds and anything slower loses the answer.
    await query.answer()

    def load():
        with session_scope() as session:
            publication, error = _editable(session, int(raw_id))
            if error:
                return None, None, error
            video = session.get(Video, publication.video_id)
            return publication, video, None

    publication, video, error = await asyncio.to_thread(load)
    if error:
        await query.message.reply_text(error)
        return
    await _ask_for(query.message, publication, field, video)


@restricted
async def on_form_reply(update: Update, _ctx) -> None:
    """Handle a reply to one of the wizard prompts."""
    message = update.message
    parent = message.reply_to_message if message else None
    if not parent or not parent.text:
        return
    # The marker is always last in the prompt. A user's own caption echoed back
    # under "Current:" could contain marker-like text, and taking the first
    # match would route the reply to whatever id that text mentioned.
    routed = route_reply(parent.text)
    if routed is None:
        return
    publication_id, field = routed
    value = (message.text or "").strip()

    with session_scope() as session:
        publication, error = _editable(session, publication_id)
        if error:
            await message.reply_text(error)
            return

        if value and value != "-":
            setattr(publication, field, value[:100] if field == "title" else value)
            if field == "caption":
                publication.description = value
        session.flush()

        video = session.get(Video, publication.video_id)
        remaining = [f for f in FIELD_ORDER[FIELD_ORDER.index(field) + 1:]
                     if not (getattr(publication, f) or "").strip()]
        if remaining:
            await _ask_for(message, publication, remaining[0], video)
            return

        # Every field answered - promote and show the approval card.
        outcome = _after_metadata_change(session, publication)
    await message.reply_text(f"✅ {FIELD_LABEL[field]} saved.\n{outcome}")


@restricted
async def cmd_help(update: Update, _ctx) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/needs - posts waiting for your wording\n"
        "/meta <id> - set Title:/Caption:/Tags: in one message\n"
        "/title <id> <text> | /caption <id> <text> | /tags <id> <tags>\n"
        "/pending - posts waiting for your approval\n"
        "/approve <id> - approve and upload\n"
        "/reject <id> - reject, never publishes\n"
        "/status - accounts and their state\n"
        "/accounts - connected accounts\n"
        "/queue - upcoming publications\n"
        "/recent - recently published\n"
        "/failed - failures needing attention\n"
        "/retry [id] - retry failed publication(s)\n"
        "/pause <account>\n"
        "/resume <account>\n"
        "/publish <account> - publish next video now\n"
        "/theme - account/theme mappings\n"
        "/report - today's summary"
    )


def build_daily_report(session, day: datetime | None = None) -> str:
    day = day or datetime.now(timezone.utc)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    lines = ["📊 DAILY REPORT", "", f"Date:\n{start.strftime('%d %b %Y')}", ""]
    totals = {"attempted": 0, "ok": 0, "failed": 0}

    for platform in (Platform.instagram, Platform.youtube):
        accounts = list(session.scalars(
            select(Account).where(Account.platform == platform).order_by(Account.id)))
        if not accounts:
            continue
        lines.append(f"{platform.value.title()}")
        lines.append("-" * len(platform.value))
        for account in accounts:
            rows = list(session.scalars(
                select(Publication).where(
                    Publication.account_id == account.id,
                    Publication.updated_at >= start,
                    Publication.updated_at < end,
                )))
            ok = sum(1 for r in rows if r.status == PublicationStatus.published)
            failed = sum(1 for r in rows
                         if r.status in (PublicationStatus.failed,
                                         PublicationStatus.failed_permanent,
                                         PublicationStatus.needs_review))
            totals["attempted"] += len(rows)
            totals["ok"] += ok
            totals["failed"] += failed
            lines += [f"@{account.username}", f"Published: {ok}",
                      f"Failed: {failed}", ""]

    lines += ["Total:", f"Attempted: {totals['attempted']}",
              f"Successful: {totals['ok']}", f"Failed: {totals['failed']}"]
    return "\n".join(lines)


def send_daily_report() -> None:
    with session_scope() as session:
        send_message(build_daily_report(session))


@restricted
async def cmd_report(update: Update, _ctx) -> None:
    with session_scope() as session:
        await update.message.reply_text(build_daily_report(session))


def main() -> None:
    from .logging_setup import setup_logging

    settings = get_settings()
    log_ = setup_logging("telegram")
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    if not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is not set - refusing to run open to all")

    app = Application.builder().token(settings.telegram_bot_token).build()
    for command, handler in [
        ("start", cmd_help), ("help", cmd_help), ("status", cmd_status),
        ("accounts", cmd_accounts), ("queue", cmd_queue), ("recent", cmd_recent),
        ("failed", cmd_failed), ("retry", cmd_retry), ("pause", cmd_pause),
        ("resume", cmd_resume), ("publish", cmd_publish), ("theme", cmd_theme),
        ("report", cmd_report), ("approve", cmd_approve), ("reject", cmd_reject),
        ("defer", cmd_defer),
        ("pending", cmd_pending), ("meta", cmd_meta), ("title", cmd_title),
        ("caption", cmd_caption), ("tags", cmd_tags), ("needs", cmd_needs),
    ]:
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(CallbackQueryHandler(on_approval_button,
                                         pattern=r"^(approve|reject|defer):\d+$"))
    app.add_handler(CallbackQueryHandler(
        on_edit_button, pattern=r"^edit:\d+:(title|caption|hashtags)$"))
    # Replies to the wizard prompts. Registered last and excluding commands so
    # it cannot swallow ordinary /commands the user types as a reply.
    app.add_handler(MessageHandler(
        filters.REPLY & filters.TEXT & ~filters.COMMAND, on_form_reply))

    async def on_error(update, context) -> None:
        """Without this python-telegram-bot only logs 'No error handlers are
        registered' and the operator is left staring at a dead button."""
        log_.exception("handler failed", exc_info=context.error)
        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat:
            try:
                await context.bot.send_message(
                    chat, f"⚠️ That action failed: {context.error}")
            except Exception:
                pass

    app.add_error_handler(on_error)

    log_.info("telegram bot polling")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
