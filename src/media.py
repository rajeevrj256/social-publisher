"""Getting the actual bytes of a video, wherever it lives.

A library of 12,000 clips is far too large to keep on disk, so videos from a
Drive source are fetched only at the moment they are needed and deleted straight
after. Everything downstream just asks for a local path and does not care which
kind of source produced it.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings
from .models import SourceKind, Video

log = logging.getLogger(__name__)


class MediaUnavailable(Exception):
    """The bytes could not be obtained; the caller should fail the publication."""


def local_path(video: Video) -> Path:
    return Path(get_settings().video_root) / video.filepath


@contextmanager
def materialize(video: Video):
    """Yield a local path to this video's bytes.

    For a local source that is the file itself, which must never be deleted.
    For a Drive source the file is downloaded to a temporary directory and
    removed on exit, so disk use stays at one video at a time.
    """
    source = video.source
    kind = source.kind if source else SourceKind.local

    if kind != SourceKind.gdrive:
        path = local_path(video)
        if not path.exists():
            raise MediaUnavailable(f"file missing on disk: {path}")
        yield path
        return

    if not video.remote_id:
        raise MediaUnavailable(f"{video.filename} has no Drive file id")

    temp_dir = Path(tempfile.mkdtemp(prefix="publisher-"))
    target = temp_dir / video.filename
    try:
        import gdown

        log.info("fetching %s from Drive", video.filename)
        result = gdown.download(id=video.remote_id, output=str(target),
                                quiet=True)
        if not result or not target.exists() or target.stat().st_size == 0:
            raise MediaUnavailable(
                f"could not download {video.filename} from Drive. The folder "
                f"must stay shared as 'Anyone with the link'.")
        yield target
    finally:
        # Always clean up, including when the upload raised.
        shutil.rmtree(temp_dir, ignore_errors=True)
