"""Ingest videos from disk into the library.

Identity is the SHA-256 of the file contents, not the path, so the same clip
re-encoded into a new folder is recognised and a moved file updates in place
instead of duplicating.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .ffmpeg_utils import probe
from .models import (
    FolderThemeMap, MetadataStatus, Source, SourceKind, Theme, Video,
)

VIDEO_SUFFIXES_TUPLE = (".mp4", ".mov", ".m4v", ".webm", ".mkv")

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_theme(session: Session, relative_path: Path) -> Theme | None:
    """Folder wins; an explicit DB mapping wins over a name match, so a folder
    can be renamed without re-tagging thousands of rows."""
    if not relative_path.parts[:-1]:
        return None
    folder = relative_path.parts[0]

    mapping = session.scalar(
        select(FolderThemeMap).where(FolderThemeMap.folder == folder))
    if mapping:
        return mapping.theme

    theme = session.scalar(select(Theme).where(func_lower(Theme.name) == folder.lower()))
    return theme


def func_lower(column):
    from sqlalchemy import func
    return func.lower(column)


DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveListingError(RuntimeError):
    """Listing failed for a reason retrying will not fix."""


def drive_folder_id(location: str) -> str | None:
    """The folder id out of any of the share-link shapes Drive hands out."""
    import re

    for pattern in (r"/folders/([A-Za-z0-9_-]+)",
                    r"[?&]id=([A-Za-z0-9_-]+)"):
        match = re.search(pattern, location)
        if match:
            return match.group(1)
    return None


def list_drive_files_api(folder_id: str, api_key: str,
                         _depth: int = 0) -> list[dict]:
    """Every file under a link-shared folder, via the official Drive API.

    gdown reads the share page and gives up over 50 files, which silently
    truncated large folders. files.list pages instead, so folder size stops
    mattering. Subfolders are walked because operators organise by batch.
    """
    import requests

    if _depth > 6:                      # a cycle would otherwise never end
        return []
    out: list[dict] = []
    token = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "key": api_key,
            "fields": "nextPageToken, files(id, name, mimeType)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        # A folder of a few thousand files spans many pages, and one dropped
        # connection would otherwise abandon the whole listing (seen live:
        # IncompleteRead on the first page, fine on retry).
        payload = None
        for attempt in range(4):
            try:
                response = requests.get(
                    "https://www.googleapis.com/drive/v3/files",
                    params=params, timeout=60)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"Drive API {response.status_code}")
                if response.status_code != 200:
                    # 400/403 are our fault (bad key, API not enabled, folder
                    # not shared); retrying cannot help.
                    raise DriveListingError(
                        f"Drive API {response.status_code}: "
                        f"{response.text[:300]}")
                payload = response.json()
                break
            except DriveListingError:
                raise
            except Exception as exc:
                if attempt == 3:
                    raise DriveListingError(
                        f"Drive listing failed after 4 attempts: {exc}") from exc
                time.sleep(2 ** attempt)
        for item in payload.get("files", []):
            if item.get("mimeType") == DRIVE_FOLDER_MIME:
                out += list_drive_files_api(item["id"], api_key, _depth + 1)
            else:
                out.append(item)
        token = payload.get("nextPageToken")
        if not token:
            break
    return out


def scan_gdrive_source(session: Session, source: Source, theme: Theme | None,
                       ) -> dict:
    """Index a shared Drive folder without downloading anything.

    Only the listing is fetched here; each video's bytes are pulled at publish
    time. Duration and dimensions are therefore unknown until then, so these
    rows stay `pending` and are validated after the just-in-time download.
    """
    stats = {"seen": 0, "added": 0, "updated": 0, "skipped": 0, "invalid": 0,
             "duplicates": 0, "error": None}

    api_key = get_settings().google_api_key
    folder_id = drive_folder_id(source.location)
    entries: list[tuple[str, str]] = []          # (file id, filename)

    if api_key and folder_id:
        try:
            entries = [(f["id"], f.get("name") or "")
                       for f in list_drive_files_api(folder_id, api_key)]
        except Exception as exc:
            log.error("Drive API listing failed for source %s: %s",
                      source.name, exc)
            stats["error"] = f"Drive API listing failed: {exc}"
            return stats
    else:
        import gdown

        try:
            listing = gdown.download_folder(url=source.location,
                                            skip_download=True, quiet=True,
                                            use_cookies=False)
        except Exception as exc:
            # gdown refuses folders over 50 files. Returning empty stats here
            # made /ingest look successful while indexing nothing, so the
            # reason is carried back instead of only logged.
            hint = ""
            if "MaximumLimit" in type(exc).__name__ or "50 files" in str(exc):
                hint = (" - this folder holds more than the 50 files gdown can "
                        "list. Set GOOGLE_API_KEY to use the Drive API, which "
                        "has no such limit.")
            log.error("could not list Drive folder for source %s: %s%s",
                      source.name, exc, hint)
            stats["error"] = f"{exc}{hint}"
            return stats
        if not listing:
            log.warning("Drive folder for source %s is empty or not shared",
                        source.name)
            stats["error"] = ("Drive returned no files: the folder is empty, or "
                              "not shared as 'Anyone with the link'.")
            return stats
        entries = [
            (getattr(i, "id", None) or getattr(i, "file_id", None),
             Path(getattr(i, "local_path", "") or getattr(i, "path", "")).name)
            for i in listing
        ]

    batch = max(1, get_settings().scan_commit_batch)
    pending = 0

    for file_id, name in entries:
        if not file_id or not name:
            continue
        if not name.lower().endswith(VIDEO_SUFFIXES_TUPLE):
            continue

        stats["seen"] += 1
        # Identity is the Drive file id: stable, and available without bytes.
        file_hash = f"gdrive:{file_id}"
        video = session.scalar(select(Video).where(Video.file_hash == file_hash))
        if video is None:
            session.add(Video(
                filename=name, filepath=name, file_hash=file_hash,
                remote_id=file_id,
                remote_url=f"https://drive.google.com/file/d/{file_id}/view",
                source_id=source.id,
                theme_id=theme.id if theme else None,
                metadata_status=MetadataStatus.pending,
            ))
            stats["added"] += 1
        else:
            changed = False
            if video.source_id != source.id:
                video.source_id, changed = source.id, True
            if theme and video.theme_id is None:
                video.theme_id, changed = theme.id, True
            if video.remote_id != file_id:
                video.remote_id, changed = file_id, True
            expected_url = f"https://drive.google.com/file/d/{file_id}/view"
            if video.remote_url != expected_url:
                video.remote_url, changed = expected_url, True
            stats["updated" if changed else "skipped"] += 1

        pending += 1
        if pending >= batch:
            # Durable progress. Committing per file would pay a round trip to
            # Neon thousands of times; committing only at the end threw the
            # whole scan away on any interruption and left the row count at 0
            # until the very last moment, which is indistinguishable from a
            # scan that is failing.
            session.commit()
            pending = 0
            log.info("indexed %s: %s/%s files so far", source.name,
                     stats["seen"], len(entries))

    session.commit()
    log.info("indexed Drive source %s: %s", source.name, stats)
    return stats


def scan_sources(session: Session, *, create_missing_themes: bool = True) -> dict:
    """Scan every enabled local source into its own slice of the library.

    Each source is scanned under its own folder and its videos are tagged with
    that source id, which is what keeps one account's link from feeding another
    account's queue.
    """
    settings = get_settings()
    totals = {"sources": 0, "seen": 0, "added": 0, "updated": 0, "skipped": 0,
              "invalid": 0, "duplicates": 0}

    sources = list(session.scalars(
        select(Source).where(Source.kind.in_([SourceKind.local,
                                              SourceKind.gdrive]),
                             Source.enabled.is_(True))))
    if not sources:
        # No sources configured: behave as a single shared library.
        return scan(session, create_missing_themes=create_missing_themes)

    for source in sources:
        root = Path(settings.video_root) / source.location
        folder = (source.name if source.kind == SourceKind.gdrive
                  else Path(source.location).name)

        # Theme for the source folder itself: an explicit mapping wins, then a
        # theme of the same name, then create one from the folder name.
        mapping = session.scalar(
            select(FolderThemeMap).where(FolderThemeMap.folder == folder))
        theme = mapping.theme if mapping else session.scalar(
            select(Theme).where(func_lower(Theme.name) == folder.lower()))
        if theme is None and create_missing_themes:
            name = folder.replace("_", " ").replace("-", " ").title()
            theme = session.scalar(select(Theme).where(Theme.name == name))
            if theme is None:
                theme = Theme(name=name,
                              description=f"Auto-created from source "
                                          f"'{source.name}'")
                session.add(theme)
                session.flush()

        if source.kind == SourceKind.gdrive:
            stats = scan_gdrive_source(session, source, theme)
        else:
            stats = scan(session, str(root),
                         create_missing_themes=create_missing_themes,
                         source_id=source.id,
                         default_theme_id=theme.id if theme else None)
        totals["sources"] += 1
        for key, value in stats.items():
            if key in totals:
                totals[key] += value
    log.info("scanned %s source(s): %s", totals["sources"], totals)
    return totals


def scan(session: Session, root: str | None = None, *,
         create_missing_themes: bool = True, source_id: int | None = None,
         default_theme_id: int | None = None) -> dict:
    """Walk the library, adding or refreshing rows. Never deletes anything:
    the files on disk are the user's originals and stay untouched."""
    settings = get_settings()
    root_path = Path(root or settings.video_root)
    stats = {"seen": 0, "added": 0, "updated": 0, "skipped": 0, "invalid": 0,
             "duplicates": 0}

    if not root_path.exists():
        log.warning("video root %s does not exist", root_path)
        return stats

    batch = max(1, settings.scan_commit_batch)
    pending = 0

    # filepath is always stored relative to VIDEO_ROOT, never to the source
    # folder: the worker, the publishers and the Telegram preview all resolve it
    # as VIDEO_ROOT / filepath, so a source-relative path points nowhere.
    library_root = Path(settings.video_root)

    for path in sorted(root_path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        stats["seen"] += 1
        relative = path.relative_to(root_path)      # for folder -> theme
        try:
            stored_path = path.relative_to(library_root)
            stored_base = library_root
        except ValueError:
            # scan() called with a root outside VIDEO_ROOT (tests, ad-hoc runs);
            # paths are then relative to that root, and the "is the original
            # still there?" check has to use the same base or every file looks
            # like it moved.
            stored_path, stored_base = relative, root_path

        try:
            file_hash = sha256_file(path)
        except OSError as exc:
            log.error("cannot hash %s: %s", relative, exc)
            stats["skipped"] += 1
            continue

        video = session.scalar(select(Video).where(Video.file_hash == file_hash))
        theme = resolve_theme(session, relative)
        if theme is None and default_theme_id is not None:
            # Files sitting directly in a source folder have no parent
            # directory to read a theme from, so the source's own folder
            # supplies it. Without this every video lands theme-less and the
            # scheduler can never match it to an account.
            theme = session.get(Theme, default_theme_id)
        if theme is None and create_missing_themes and len(relative.parts) > 1:
            name = relative.parts[0].replace("_", " ").replace("-", " ").title()
            theme = session.scalar(select(Theme).where(Theme.name == name))
            if theme is None:
                theme = Theme(name=name, description=f"Auto-created from folder "
                                                     f"'{relative.parts[0]}'")
                session.add(theme)
                session.flush()

        if video is None:
            info = probe(path)
            video = Video(
                filename=path.name,
                filepath=str(stored_path),
                file_hash=file_hash,
                theme_id=theme.id if theme else None,
                duration=info.duration, width=info.width, height=info.height,
                fps=info.fps, file_size=info.file_size, codec=info.codec,
                metadata_status=MetadataStatus.ok if info.ok else MetadataStatus.invalid,
                metadata_error=info.error,
                source_id=source_id,
            )
            session.add(video)
            stats["added"] += 1
            if not info.ok:
                stats["invalid"] += 1
        else:
            changed = False
            if video.filepath != str(stored_path):
                previous = stored_base / video.filepath
                if previous.exists():
                    # The same bytes exist in two places. Treat the known row as
                    # canonical: re-theming it on every scan would make the
                    # video's theme depend on directory iteration order.
                    log.info("duplicate content: %s mirrors %s",
                             stored_path, video.filepath)
                    stats["duplicates"] = stats.get("duplicates", 0) + 1
                    continue
                # The original is gone, so this is a genuine move.
                video.filepath = str(stored_path)
                video.filename = path.name
                if theme and video.theme_id != theme.id:
                    video.theme_id = theme.id
                changed = True
            elif theme and video.theme_id is None:
                video.theme_id = theme.id
                changed = True
            if source_id is not None and video.source_id != source_id:
                video.source_id = source_id
                changed = True
            if video.metadata_status != MetadataStatus.ok:
                info = probe(path)
                video.duration, video.width, video.height = (
                    info.duration, info.width, info.height)
                video.fps, video.file_size, video.codec = (
                    info.fps, info.file_size, info.codec)
                video.metadata_status = (
                    MetadataStatus.ok if info.ok else MetadataStatus.invalid)
                video.metadata_error = info.error
                changed = True
            stats["updated" if changed else "skipped"] += 1

        pending += 1
        if pending >= batch:
            # Same reasoning as the Drive scan: a local walk also probes every
            # file with ffprobe, so an interrupted run is expensive to redo.
            session.commit()
            pending = 0
            log.info("scanned %s files so far", stats["seen"])

    session.commit()
    log.info("scan complete: %s", stats)
    return stats
