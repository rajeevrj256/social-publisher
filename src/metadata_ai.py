"""Per-account title/caption/hashtag generation.

Metadata belongs to the publication, not the video: the same clip posted by
@motivation_daily and @fitness_clips must not read as the same author, so the
account's own prompt and hashtag sets drive every field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Account, HashtagSet, Video

log = logging.getLogger(__name__)


@dataclass
class GeneratedMetadata:
    title: str
    caption: str
    hashtags: str
    description: str | None = None
    # False when the values are only a filename fallback, i.e. nothing a human
    # or a model actually wrote. Publishing those unreviewed is how an account
    # ends up captioned "Motivation001".
    authored: bool = False


def _fallback_title(video: Video) -> str:
    stem = video.filename.rsplit(".", 1)[0]
    return stem.replace("_", " ").replace("-", " ").strip().title()[:100]


def account_hashtags(session: Session, account: Account, video: Video) -> str:
    """Theme-specific set wins over the account default; both are optional."""
    rows = list(session.scalars(
        select(HashtagSet).where(
            HashtagSet.account_id == account.id,
            or_(HashtagSet.theme_id == video.theme_id,
                HashtagSet.theme_id.is_(None)),
        )
    ))
    specific = next((r for r in rows if r.theme_id == video.theme_id), None)
    default = next((r for r in rows if r.theme_id is None), None)
    chosen = specific or default
    return chosen.hashtags.strip() if chosen else ""


def generate(session: Session, account: Account, video: Video) -> GeneratedMetadata:
    """Templates and stored hashtags always work; the model is an enhancement
    layered on top, never a hard dependency of publishing."""
    config = account.ai_config
    theme_name = video.theme.name if video.theme else "general"
    hashtags = account_hashtags(session, account, video)

    title = _fallback_title(video)
    caption = ""
    authored = False
    if config and config.title_template:
        title = config.title_template.format(
            title=title, theme=theme_name, filename=video.filename)[:100]
    if config and config.caption_template:
        caption = config.caption_template.format(
            title=title, theme=theme_name, filename=video.filename)
        authored = True  # a template is an explicit editorial decision

    settings = get_settings()
    if config and config.ai_enabled and settings.anthropic_api_key:
        try:
            ai = _generate_with_model(config, theme_name, video, hashtags)
            if ai:
                title = ai.get("title", title)[:100]
                caption = ai.get("caption", caption)
                if ai.get("hashtags"):
                    hashtags = ai["hashtags"]
                authored = True
        except Exception as exc:
            # A model outage must never stop the post going out.
            log.warning("AI metadata failed for @%s, using templates: %s",
                        account.username, exc)

    return GeneratedMetadata(title=title, caption=caption, hashtags=hashtags,
                             description=caption, authored=authored)


def metadata_complete(publication) -> list[str]:
    """Fields still missing before this may be published. Empty list means ready."""
    missing = []
    if not (publication.title or "").strip():
        missing.append("title")
    if not (publication.caption or "").strip():
        missing.append("caption")
    return missing


def _generate_with_model(config, theme_name: str, video: Video,
                         hashtags: str) -> dict | None:
    import json

    import anthropic

    settings = get_settings()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    system = config.system_prompt or (
        "You write short, natural social media captions. No emoji spam, "
        "no clickbait, no invented facts about the video.")
    rules = config.hashtag_rules or "Return 3-6 relevant hashtags."

    prompt = (
        f"Theme: {theme_name}\n"
        f"Video file: {video.filename}\n"
        f"Duration: {video.duration or 'unknown'}s\n"
        f"Existing default hashtags: {hashtags or 'none'}\n\n"
        f"Hashtag rules: {rules}\n\n"
        "Reply with JSON only: "
        '{"title": "...", "caption": "...", "hashtags": "#a #b #c"}'
    )
    message = client.messages.create(
        model=settings.ai_model,
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in message.content
                   if getattr(block, "type", "") == "text").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    return json.loads(text)
