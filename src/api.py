"""Admin mapping API.

Not a dashboard - there is no HTML, no JavaScript and no UI. It exists so
mappings can be POSTed in bulk instead of typed one CLI call at a time, which
matters when wiring 20 accounts to their own source links.

Protected by a bearer token and bound to localhost by default: this API can
change what gets published, so it is never exposed publicly.
"""

from __future__ import annotations

import logging
import secrets
from datetime import time as dtime

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .config import get_settings
from .crypto import encrypt
from .db import session_scope
from .models import (
    Account, AccountCredential, AccountSchedule, AccountSource, AccountTheme,
    Platform, Schedule, ScheduleAccount, Source, SourceKind, Theme, Video,
    VideoAccountMapping,
)

log = logging.getLogger(__name__)
app = FastAPI(title="Social Publisher admin API", docs_url=None, redoc_url=None)


def require_token(authorization: str = Header(default="")) -> None:
    """Constant-time comparison: a token check that leaks timing is not a check."""
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "ADMIN_API_TOKEN is not configured")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")


# ---------------------------------------------------------------- schemas

class SourceIn(BaseModel):
    name: str
    location: str = Field(description="folder under VIDEO_ROOT, or https base URL")
    kind: SourceKind = SourceKind.local
    description: str | None = None


class AccountIn(BaseModel):
    platform: Platform
    username: str
    account_name: str
    platform_account_id: str | None = None
    timezone: str = "Asia/Kolkata"
    require_approval: bool = True


class ScheduleIn(BaseModel):
    publish_time: str = Field(description="HH:MM, local to the account")
    videos_per_day: int = 1
    day_of_week: int | None = None
    timezone: str | None = None


class OAuthClientIn(BaseModel):
    """OAuth *app* credentials. Write-only: never returned by any endpoint."""
    client_id: str
    client_secret: str


class ThemeIn(BaseModel):
    name: str
    description: str | None = None
    coordinated: bool = Field(
        default=True,
        description="default true: every account on this theme publishes the "
                    "SAME video each cycle (each still approves its own "
                    "caption). Set false for independent per-account queues.")


class IngestIn(BaseModel):
    """One call to add a Drive folder to an account.

    The theme must already be registered via POST /themes. Ingest deliberately
    will not invent one: a typo would otherwise create a silent second theme
    that no account is mapped to, and those videos would never be published.
    """
    account: str | list[str] = Field(
        description="one account or a list of them; id (stable) or "
                    "username/display name. The same theme and Drive folder is "
                    "wired to every account listed.")
    theme: str = Field(description="must already exist; register via POST /themes")
    drive_url: str = Field(description="shared Google Drive folder link")
    source_name: str | None = Field(
        default=None, description="defaults to a slug of the theme")
    priority: int = Field(default=10, description="weight against other themes")
    replace_sources: bool = Field(
        default=False,
        description="true drops the account's other sources; default adds")


class SharedScheduleIn(BaseModel):
    """A slot owned by several accounts, fired once for all of them."""
    name: str
    publish_time: str = Field(description="HH:MM in this schedule's timezone")
    accounts: list[str] = Field(description="ids or usernames")
    videos_per_day: int = 1
    day_of_week: int | None = Field(default=None, description="0=Mon .. 6=Sun")
    timezone: str = "Asia/Kolkata"
    theme: str | None = Field(default=None,
                              description="restrict this slot to one theme")


class MappingIn(BaseModel):
    """One account wired to its sources, themes and schedule."""
    account: str = Field(description="username or account_name")
    sources: list[str] = Field(default_factory=list)
    themes: dict[str, int] = Field(default_factory=dict,
                                   description='{"Motivation": 10}')
    schedules: list[ScheduleIn] = Field(default_factory=list)


class BulkIn(BaseModel):
    sources: list[SourceIn] = Field(default_factory=list)
    accounts: list[AccountIn] = Field(default_factory=list)
    mappings: list[MappingIn] = Field(default_factory=list)
    # {"youtube_1": {"client_id": "...", "client_secret": "..."}}
    oauth_clients: dict[str, OAuthClientIn] = Field(default_factory=dict)


# ---------------------------------------------------------------- helpers

def _account(session, token: str | int) -> Account:
    """Resolve an account by numeric id, username, or display name.

    The id is accepted first because it is the only stable handle: a username
    can be renamed (this one already was), and a stored payload referencing the
    old name would then silently 404.
    """
    text = str(token).strip()
    account = None
    if text.isdigit():
        account = session.get(Account, int(text))
    if account is None:
        account = session.scalar(
            select(Account).where(func.lower(Account.username) == text.lower())
        ) or session.scalar(
            select(Account).where(func.lower(Account.account_name) == text.lower()))
    if not account:
        raise HTTPException(404, f"account {token!r} not found")
    return account


def _source(session, name: str) -> Source:
    source = session.scalar(select(Source).where(Source.name == name))
    if not source:
        raise HTTPException(404, f"source {name!r} not found")
    return source


def _theme(session, name: str) -> Theme:
    theme = session.scalar(select(Theme).where(Theme.name == name))
    if not theme:
        theme = Theme(name=name)
        session.add(theme)
        session.flush()
    return theme


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/sources", dependencies=[Depends(require_token)])
def create_source(payload: SourceIn) -> dict:
    with session_scope() as session:
        existing = session.scalar(select(Source).where(Source.name == payload.name))
        if existing:
            existing.location = payload.location
            existing.kind = payload.kind
            existing.description = payload.description
            session.flush()
            return {"id": existing.id, "name": existing.name, "updated": True}
        source = Source(**payload.model_dump())
        session.add(source)
        session.flush()
        return {"id": source.id, "name": source.name, "updated": False}


@app.get("/sources", dependencies=[Depends(require_token)])
def list_sources() -> list[dict]:
    with session_scope() as session:
        output = []
        for source in session.scalars(select(Source).order_by(Source.id)):
            theme = (session.get(Theme, source.videos_theme_id)
                     if source.videos_theme_id else None)
            count = session.scalar(
                select(func.count()).select_from(Video)
                .where(Video.source_id == source.id))
            output.append({"id": source.id, "name": source.name,
                           "kind": source.kind.value, "location": source.location,
                           "enabled": source.enabled,
                           "theme": theme.name if theme else None,
                           "videos": count})
        return output


@app.get("/themes", dependencies=[Depends(require_token)])
def list_themes() -> list[dict]:
    """The set of valid theme values - the allowed list /ingest checks against."""
    with session_scope() as session:
        output = []
        for theme in session.scalars(select(Theme).order_by(Theme.name)):
            count = session.scalar(select(func.count()).select_from(Video)
                                   .where(Video.theme_id == theme.id))
            output.append({"id": theme.id, "name": theme.name,
                           "active": theme.active,
                           "coordinated": theme.coordinated, "videos": count,
                           "description": theme.description})
        return output


@app.post("/themes", dependencies=[Depends(require_token)])
def create_theme(payload: ThemeIn) -> dict:
    """Register a new theme. This is the only way a theme comes into existence."""
    with session_scope() as session:
        existing = session.scalar(select(Theme).where(Theme.name == payload.name))
        if existing:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"theme {payload.name!r} already exists (id {existing.id})")
        theme = Theme(name=payload.name, description=payload.description,
                      coordinated=payload.coordinated)
        session.add(theme)
        session.flush()
        return {"id": theme.id, "name": theme.name,
                "coordinated": theme.coordinated, "created": True}


@app.patch("/themes/{name}", dependencies=[Depends(require_token)])
def update_theme(name: str, coordinated: bool | None = None,
                 active: bool | None = None) -> dict:
    with session_scope() as session:
        theme = session.scalar(select(Theme).where(Theme.name == name))
        if theme is None:
            raise HTTPException(404, f"theme {name!r} not found")
        if coordinated is not None:
            theme.coordinated = coordinated
        if active is not None:
            theme.active = active
        session.flush()
        return {"name": theme.name, "coordinated": theme.coordinated,
                "active": theme.active}


@app.post("/accounts", dependencies=[Depends(require_token)])
def create_account(payload: AccountIn) -> dict:
    with session_scope() as session:
        existing = session.scalar(select(Account).where(
            Account.platform == payload.platform,
            Account.username == payload.username))
        if existing:
            return {"id": existing.id, "username": existing.username,
                    "created": False}
        account = Account(**payload.model_dump())
        session.add(account)
        session.flush()
        return {"id": account.id, "username": account.username, "created": True}


@app.post("/accounts/{account}/sources", dependencies=[Depends(require_token)])
def map_account_sources(account: str, sources: list[str]) -> dict:
    """Replace the account's source list. This is the endpoint that makes
    'youtube account 1 uploads from link 1' true."""
    with session_scope() as session:
        target = _account(session, account)
        for row in session.scalars(select(AccountSource).where(
                AccountSource.account_id == target.id)):
            session.delete(row)
        session.flush()
        for name in sources:
            source = _source(session, name)
            session.add(AccountSource(account_id=target.id, source_id=source.id))
        session.flush()
        return {"account": target.username, "sources": sources}


@app.post("/accounts/{account}/themes", dependencies=[Depends(require_token)])
def map_account_themes(account: str, themes: dict[str, int]) -> dict:
    with session_scope() as session:
        target = _account(session, account)
        for row in session.scalars(select(AccountTheme).where(
                AccountTheme.account_id == target.id)):
            session.delete(row)
        session.flush()
        for name, priority in themes.items():
            theme = _theme(session, name)
            session.add(AccountTheme(account_id=target.id, theme_id=theme.id,
                                     priority=priority))
        session.flush()
        return {"account": target.username, "themes": themes}


@app.post("/accounts/{account}/schedules", dependencies=[Depends(require_token)])
def set_schedules(account: str, schedules: list[ScheduleIn]) -> dict:
    with session_scope() as session:
        target = _account(session, account)
        for row in session.scalars(select(AccountSchedule).where(
                AccountSchedule.account_id == target.id)):
            session.delete(row)
        session.flush()
        for item in schedules:
            hour, _, minute = item.publish_time.partition(":")
            session.add(AccountSchedule(
                account_id=target.id, publish_time=dtime(int(hour), int(minute or 0)),
                videos_per_day=item.videos_per_day, day_of_week=item.day_of_week,
                timezone=item.timezone))
        session.flush()
        return {"account": target.username, "schedules": len(schedules)}


@app.post("/videos/{video_id}/accounts", dependencies=[Depends(require_token)])
def map_video(video_id: int, accounts: list[str]) -> dict:
    """Pin one video to specific accounts. Once any mapping exists the mapping
    is authoritative, so this narrows eligibility rather than widening it."""
    with session_scope() as session:
        video = session.get(Video, video_id)
        if not video:
            raise HTTPException(404, f"video {video_id} not found")
        for name in accounts:
            target = _account(session, name)
            exists = session.scalar(select(VideoAccountMapping).where(
                VideoAccountMapping.video_id == video_id,
                VideoAccountMapping.account_id == target.id))
            if not exists:
                session.add(VideoAccountMapping(video_id=video_id,
                                                account_id=target.id))
        session.flush()
        return {"video": video.filename, "accounts": accounts}


@app.post("/accounts/{account}/oauth-client", dependencies=[Depends(require_token)])
def set_oauth_client(account: str, payload: OAuthClientIn) -> dict:
    """Store this account's own OAuth app credentials.

    Different Google accounts are different Cloud projects with different client
    ids; refreshing against the wrong one fails with an opaque invalid_client.
    The secret is encrypted at rest and never returned by any endpoint.
    """
    with session_scope() as session:
        target = _account(session, account)
        credential = target.credentials or AccountCredential(account_id=target.id)
        credential.client_id = payload.client_id
        credential.client_secret_encrypted = encrypt(payload.client_secret)
        session.add(credential)
        session.flush()
        return {"account": target.username, "client_id": payload.client_id,
                "client_secret": "stored (encrypted)"}


@app.get("/schedules", dependencies=[Depends(require_token)])
def list_schedules() -> list[dict]:
    with session_scope() as session:
        output = []
        for schedule in session.scalars(select(Schedule).order_by(Schedule.id)):
            members = [session.get(Account, row.account_id).username
                       for row in session.scalars(select(ScheduleAccount).where(
                           ScheduleAccount.schedule_id == schedule.id))]
            theme = (session.get(Theme, schedule.theme_id)
                     if schedule.theme_id else None)
            output.append({
                "id": schedule.id, "name": schedule.name,
                "publish_time": schedule.publish_time.strftime("%H:%M"),
                "videos_per_day": schedule.videos_per_day,
                "day_of_week": schedule.day_of_week,
                "timezone": schedule.timezone, "enabled": schedule.enabled,
                "theme": theme.name if theme else None,
                "accounts": members,
            })
        return output


@app.post("/schedules", dependencies=[Depends(require_token)])
def create_schedule(payload: SharedScheduleIn) -> dict:
    """One schedule, many accounts: fires once and hands the same video to all.

    This is what guarantees the shared video. Separate per-account schedules
    fire at their own times, and each fan-out reaches the others, so every
    account ends up publishing once per member schedule.
    """
    with session_scope() as session:
        targets = [_account(session, token) for token in payload.accounts]
        if not targets:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "accounts must not be empty")

        theme = None
        if payload.theme:
            theme = session.scalar(select(Theme).where(Theme.name == payload.theme))
            if theme is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"unknown theme {payload.theme!r}")

        hour, _, minute = payload.publish_time.partition(":")
        schedule = session.scalar(select(Schedule).where(
            Schedule.name == payload.name))
        if schedule is None:
            schedule = Schedule(name=payload.name)
            session.add(schedule)
        schedule.publish_time = dtime(int(hour), int(minute or 0))
        schedule.videos_per_day = payload.videos_per_day
        schedule.day_of_week = payload.day_of_week
        schedule.timezone = payload.timezone
        schedule.theme_id = theme.id if theme else None
        schedule.enabled = True
        session.flush()

        # Replace membership so re-posting a corrected list cannot leave a
        # stale account attached.
        for row in session.scalars(select(ScheduleAccount).where(
                ScheduleAccount.schedule_id == schedule.id)):
            session.delete(row)
        session.flush()
        for target in targets:
            session.add(ScheduleAccount(schedule_id=schedule.id,
                                        account_id=target.id))

        # Per-account rows for these accounts would double-schedule them.
        removed = 0
        for target in targets:
            for row in session.scalars(select(AccountSchedule).where(
                    AccountSchedule.account_id == target.id)):
                session.delete(row)
                removed += 1
        session.flush()

        return {"id": schedule.id, "name": schedule.name,
                "publish_time": schedule.publish_time.strftime("%H:%M"),
                "accounts": [t.username for t in targets],
                "theme": theme.name if theme else None,
                "per_account_schedules_removed": removed,
                "note": "This slot fires once and gives the same video to every "
                        "account listed. Each still approves its own caption."}


@app.delete("/schedules/{name}", dependencies=[Depends(require_token)])
def delete_schedule(name: str) -> dict:
    with session_scope() as session:
        schedule = session.scalar(select(Schedule).where(Schedule.name == name))
        if schedule is None:
            raise HTTPException(404, f"schedule {name!r} not found")
        session.delete(schedule)
        return {"deleted": name}


@app.post("/ingest", dependencies=[Depends(require_token)])
def ingest(payload: IngestIn) -> dict:
    """Add a Drive folder of videos to an account in one call.

    Indexing is queued rather than done inline: listing a folder of several
    hundred files takes minutes, which no HTTP client should be asked to wait
    through.
    """
    from .worker import get_queue

    created = {"theme": False, "source": False, "theme_mapped": False,
               "source_mapped": False}

    tokens = (payload.account if isinstance(payload.account, list)
              else [payload.account])
    if not tokens:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "account must not be empty")

    with session_scope() as session:
        # Resolve every account before changing anything: a typo in the third
        # entry should not leave the first two half-wired.
        targets = [_account(session, token) for token in tokens]

        theme = session.scalar(select(Theme).where(Theme.name == payload.theme))
        if theme is None:
            # Refused rather than created: an unrecognised theme is almost
            # always a typo, and inventing one here would quietly park the
            # videos under a theme no account publishes from.
            valid = [t.name for t in session.scalars(
                select(Theme).where(Theme.active.is_(True)).order_by(Theme.name))]
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                {"error": f"unknown theme {payload.theme!r}",
                 "valid_themes": valid,
                 "hint": "register it first: POST /themes "
                         '{"name": "' + payload.theme + '"}'})
        if not theme.active:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"theme {theme.name!r} is inactive")

        name = payload.source_name or (
            "drive_" + payload.theme.lower().replace(" ", "_").replace("-", "_"))
        source = session.scalar(select(Source).where(Source.name == name))
        if source is None:
            source = Source(name=name, kind=SourceKind.gdrive,
                            location=payload.drive_url,
                            videos_theme_id=theme.id,
                            description=f"Drive folder for {payload.theme}")
            session.add(source)
            session.flush()
            created["source"] = True
        else:
            # Re-ingesting the same name repoints it rather than duplicating.
            source.location = payload.drive_url
            source.videos_theme_id = theme.id
            source.enabled = True

        wired = []
        for target in targets:
            if payload.replace_sources:
                for row in session.scalars(select(AccountSource).where(
                        AccountSource.account_id == target.id)):
                    session.delete(row)
                session.flush()

            link = session.scalar(select(AccountSource).where(
                AccountSource.account_id == target.id,
                AccountSource.source_id == source.id))
            source_mapped = link is None
            if link is None:
                session.add(AccountSource(account_id=target.id,
                                          source_id=source.id))
            else:
                link.enabled = True

            mapping = session.scalar(select(AccountTheme).where(
                AccountTheme.account_id == target.id,
                AccountTheme.theme_id == theme.id))
            theme_mapped = mapping is None
            if mapping is None:
                session.add(AccountTheme(account_id=target.id, theme_id=theme.id,
                                         priority=payload.priority))
            else:
                mapping.enabled = True
                mapping.priority = payload.priority

            wired.append({"id": target.id, "username": target.username,
                          "platform": target.platform.value,
                          "theme_mapped": theme_mapped,
                          "source_mapped": source_mapped})
        session.flush()

        source_id, source_name = source.id, source.name
        theme_name = theme.name
        coordinated = theme.coordinated

    job = get_queue().enqueue("src.jobs.index_source", source_id,
                              job_timeout=3600, result_ttl=86400)
    return {
        "accounts": wired,
        "theme": theme_name,
        "coordinated": coordinated,
        "source": source_name,
        "source_created": created["source"],
        "indexing": "queued",
        "job_id": job.id,
        "note": ("Indexing runs in the background; a Telegram message reports "
                 "the result. " + ("This theme is coordinated: every account "
                                   "above publishes the same video each cycle, "
                                   "each approving its own caption."
                                   if coordinated else
                                   "This theme is not coordinated: each account "
                                   "picks independently.")),
    }


@app.post("/bulk", dependencies=[Depends(require_token)])
def bulk_import(payload: BulkIn) -> dict:
    """Wire the whole system in one POST: sources, accounts and every mapping."""
    result = {"sources": 0, "accounts": 0, "mappings": 0}
    with session_scope() as session:
        for item in payload.sources:
            existing = session.scalar(select(Source).where(Source.name == item.name))
            if existing:
                existing.location, existing.kind = item.location, item.kind
            else:
                session.add(Source(**item.model_dump()))
            result["sources"] += 1
        session.flush()

        for item in payload.accounts:
            existing = session.scalar(select(Account).where(
                Account.platform == item.platform,
                Account.username == item.username))
            if not existing:
                session.add(Account(**item.model_dump()))
                result["accounts"] += 1
        session.flush()

        for mapping in payload.mappings:
            target = _account(session, mapping.account)

            if mapping.sources:
                for row in session.scalars(select(AccountSource).where(
                        AccountSource.account_id == target.id)):
                    session.delete(row)
                session.flush()
                for name in mapping.sources:
                    session.add(AccountSource(account_id=target.id,
                                              source_id=_source(session, name).id))
            if mapping.themes:
                for row in session.scalars(select(AccountTheme).where(
                        AccountTheme.account_id == target.id)):
                    session.delete(row)
                session.flush()
                for name, priority in mapping.themes.items():
                    session.add(AccountTheme(account_id=target.id,
                                             theme_id=_theme(session, name).id,
                                             priority=priority))
            if mapping.schedules:
                for row in session.scalars(select(AccountSchedule).where(
                        AccountSchedule.account_id == target.id)):
                    session.delete(row)
                session.flush()
                for item in mapping.schedules:
                    hour, _, minute = item.publish_time.partition(":")
                    session.add(AccountSchedule(
                        account_id=target.id,
                        publish_time=dtime(int(hour), int(minute or 0)),
                        videos_per_day=item.videos_per_day,
                        day_of_week=item.day_of_week, timezone=item.timezone))
            result["mappings"] += 1

        for username, client in payload.oauth_clients.items():
            target = _account(session, username)
            credential = target.credentials or AccountCredential(
                account_id=target.id)
            credential.client_id = client.client_id
            credential.client_secret_encrypted = encrypt(client.client_secret)
            session.add(credential)
            result["oauth_clients"] = result.get("oauth_clients", 0) + 1
        session.flush()
    return result


@app.get("/mapping", dependencies=[Depends(require_token)])
def show_mapping() -> list[dict]:
    """The full account -> sources/themes picture, for verifying a bulk import."""
    with session_scope() as session:
        output = []
        for account in session.scalars(select(Account).order_by(Account.id)):
            sources = [session.get(Source, row.source_id).name
                       for row in session.scalars(select(AccountSource).where(
                           AccountSource.account_id == account.id))]
            themes = {session.get(Theme, row.theme_id).name: row.priority
                      for row in session.scalars(select(AccountTheme).where(
                          AccountTheme.account_id == account.id))}
            credential = account.credentials
            output.append({
                "id": account.id, "platform": account.platform.value,
                "username": account.username, "enabled": account.enabled,
                "require_approval": account.require_approval,
                "sources": sources, "themes": themes,
                # Presence only. Secrets never leave the database.
                "oauth_client_id": credential.client_id if credential else None,
                "has_client_secret": bool(
                    credential and credential.client_secret_encrypted),
                "has_access_token": bool(
                    credential and credential.access_token_encrypted),
                "has_refresh_token": bool(
                    credential and credential.refresh_token_encrypted),
            })
        return output


def main() -> None:
    import uvicorn

    from .logging_setup import setup_logging

    settings = get_settings()
    setup_logging("api")
    if not settings.admin_api_token:
        raise SystemExit("ADMIN_API_TOKEN is not set - refusing to start open")
    uvicorn.run(app, host=settings.admin_api_host, port=settings.admin_api_port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
