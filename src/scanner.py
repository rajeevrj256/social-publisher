"""Ingest videos from disk into the library.

Identity is the SHA-256 of the file contents, not the path, so the same clip
re-encoded into a new folder is recognised and a moved file updates in place
instead of duplicating.
"""

from __future__ import annotations

import hashlib
import logging
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


def scan_gdrive_source(session: Session, source: Source, theme: Theme | None,
                       ) -> dict:
    """Index a shared Drive folder without downloading anything.

    Only the listing is fetched here; each video's bytes are pulled at publish
    time. Duration and dimensions are therefore unknown until then, so these
    rows stay `pending` and are validated after the just-in-time download.
    """
    import gdown

    stats = {"seen": 0, "added": 0, "updated": 0, "skipped": 0, "invalid": 0,
             "duplicates": 0}
    try:
        listing = gdown.download_folder(url=source.location, skip_download=True,
                                        quiet=True, use_cookies=False)
    except Exception as exc:
        log.error("could not list Drive folder for source %s: %s",
                  source.name, exc)
        return stats
    if not listing:
        log.warning("Drive folder for source %s is empty or not shared",
                    source.name)
        return stats

    for item in listing:
        # gdown returns objects carrying the file id and its local-relative path.
        file_id = getattr(item, "id", None) or getattr(item, "file_id", None)
        name = Path(getattr(item, "local_path", "") or getattr(item, "path", "")).name
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

    session.flush()
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

    session.flush()
    log.info("scan complete: %s", stats)
    return stats
