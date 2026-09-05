"""Ingestion: hashing, folder→theme, and idempotency."""

import hashlib
from pathlib import Path

from sqlalchemy import select

from src.models import FolderThemeMap, Theme, Video
from src.scanner import resolve_theme, scan, sha256_file


def _write(root: Path, relative: str, content: bytes = b"fake video") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_sha256_matches_hashlib(tmp_path):
    path = _write(tmp_path, "a.mp4", b"contents")
    assert sha256_file(path) == hashlib.sha256(b"contents").hexdigest()


def test_folder_maps_to_theme_via_table(session, themes, tmp_path):
    session.add(FolderThemeMap(folder="motivation",
                               theme_id=themes["Motivation"].id))
    session.flush()
    theme = resolve_theme(session, Path("motivation/clip.mp4"))
    assert theme is not None and theme.name == "Motivation"


def test_scan_adds_videos_and_creates_missing_themes(session, tmp_path):
    # Distinct bytes: identical content is deliberately deduplicated.
    _write(tmp_path, "motivation/one.mp4", b"clip one")
    _write(tmp_path, "gym/two.mp4", b"clip two")

    stats = scan(session, str(tmp_path))
    assert stats["added"] == 2, stats

    names = {t.name for t in session.scalars(select(Theme))}
    assert {"Motivation", "Gym"} <= names


def test_rescan_does_not_duplicate(session, tmp_path):
    """Re-running the scanner is routine; it must never double the library."""
    _write(tmp_path, "motivation/one.mp4")
    scan(session, str(tmp_path))
    stats = scan(session, str(tmp_path))

    assert stats["added"] == 0
    assert session.scalar(select(Theme).where(Theme.name == "Motivation")) is not None
    assert len(list(session.scalars(select(Video)))) == 1


def test_moved_file_updates_path_instead_of_duplicating(session, tmp_path):
    """Identity is the content hash, so a reorganised folder is not a new video."""
    _write(tmp_path, "motivation/one.mp4", b"same bytes")
    scan(session, str(tmp_path))
    (tmp_path / "motivation" / "one.mp4").unlink()
    _write(tmp_path, "success/one.mp4", b"same bytes")

    scan(session, str(tmp_path))
    videos = list(session.scalars(select(Video)))
    assert len(videos) == 1, "same bytes must remain one row"
    assert videos[0].filepath == "success/one.mp4"


def test_duplicate_content_keeps_the_original_theme(session, tmp_path):
    """The same bytes in two theme folders must not flip the theme each scan."""
    _write(tmp_path, "motivation/one.mp4", b"identical")
    scan(session, str(tmp_path))
    original = session.scalar(select(Video))
    original_theme = original.theme_id

    _write(tmp_path, "gym/copy.mp4", b"identical")
    stats = scan(session, str(tmp_path))

    videos = list(session.scalars(select(Video)))
    assert len(videos) == 1, "identical content stays one row"
    assert videos[0].theme_id == original_theme, "theme must not flip"
    assert stats["duplicates"] == 1


def test_non_video_files_are_ignored(session, tmp_path):
    _write(tmp_path, "motivation/notes.txt", b"hello")
    _write(tmp_path, "motivation/cover.jpg", b"img")
    stats = scan(session, str(tmp_path))
    assert stats["seen"] == 0
