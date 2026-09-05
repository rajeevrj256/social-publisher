"""Listing a Drive folder must not fail quietly.

A folder over gdown's 50-file cap returned all-zero stats, so `/ingest`
answered `"indexing": "queued"`, Telegram reported `Added: 0`, and nothing
distinguished that from an empty folder. The reason now travels with the stats.
"""

import pytest

from src.scanner import drive_folder_id


@pytest.mark.parametrize("url,expected", [
    ("https://drive.google.com/drive/folders/1s9lS4Ab3RmBR2wyMzf9LCdQ3oA--z3Nn?usp=sharing",
     "1s9lS4Ab3RmBR2wyMzf9LCdQ3oA--z3Nn"),
    ("https://drive.google.com/drive/folders/1MBqB1Rg_cr8zZUJ8Xt_fBBxo9IRmM5j6",
     "1MBqB1Rg_cr8zZUJ8Xt_fBBxo9IRmM5j6"),
    ("https://drive.google.com/open?id=1AbC-dEf_123", "1AbC-dEf_123"),
    ("not a drive url", None),
])
def test_folder_id_is_extracted_from_every_share_shape(url, expected):
    assert drive_folder_id(url) == expected


def _force_gdown_fallback(monkeypatch, scanner):
    """Pretend no Drive key is configured.

    Clearing the env var is not enough: settings also read the real .env, so a
    developer with a working key would silently exercise the API path and these
    fallback assertions would never run.
    """
    real = scanner.get_settings

    def no_key():
        settings = real()
        return type("S", (), {**{k: getattr(settings, k)
                                 for k in ("google_api_key",)},
                              "google_api_key": None})()

    monkeypatch.setattr(scanner, "get_settings", no_key)


class _FakeSource:
    id = 1
    name = "drive_luxury_theme"
    location = "https://drive.google.com/drive/folders/ABC123"


def test_gdown_file_cap_is_reported_not_swallowed(monkeypatch):
    """The exact failure that made an ingest look successful."""
    import gdown

    from src import scanner

    _force_gdown_fallback(monkeypatch, scanner)

    class FolderContentsMaximumLimitError(Exception):
        pass

    def boom(**_kwargs):
        raise FolderContentsMaximumLimitError(
            "The gdrive folder with url: ... has more than 50 files, "
            "gdrive can't download more than this limit.")

    monkeypatch.setattr(gdown, "download_folder", boom)

    stats = scanner.scan_gdrive_source(None, _FakeSource(), None)

    assert stats["seen"] == 0
    assert stats["error"], "the reason must travel back with the stats"
    assert "50 files" in stats["error"]
    assert "GOOGLE_API_KEY" in stats["error"], \
        "the message must say how to fix it"


def test_empty_folder_is_reported_too(monkeypatch):
    import gdown

    from src import scanner

    _force_gdown_fallback(monkeypatch, scanner)
    monkeypatch.setattr(gdown, "download_folder", lambda **_k: [])

    stats = scanner.scan_gdrive_source(None, _FakeSource(), None)

    assert stats["error"] and "not shared" in stats["error"]


def test_api_listing_pages_and_walks_subfolders(monkeypatch):
    """Paging is the whole point: one page would silently truncate again."""
    from src import scanner

    pages = {
        (None, "root"): {"files": [
            {"id": "f1", "name": "a.mp4", "mimeType": "video/mp4"},
            {"id": "sub", "name": "batch2",
             "mimeType": scanner.DRIVE_FOLDER_MIME},
        ], "nextPageToken": "p2"},
        ("p2", "root"): {"files": [
            {"id": "f2", "name": "b.mp4", "mimeType": "video/mp4"}]},
        (None, "sub"): {"files": [
            {"id": "f3", "name": "c.mp4", "mimeType": "video/mp4"}]},
    }

    class _Response:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    def fake_get(_url, params=None, timeout=None):
        parent = params["q"].split("'")[1]
        return _Response(pages[(params.get("pageToken"), parent)])

    import requests
    monkeypatch.setattr(requests, "get", fake_get)

    files = scanner.list_drive_files_api("root", "KEY")

    assert [f["id"] for f in files] == ["f1", "f3", "f2"], \
        "must follow nextPageToken and recurse into subfolders"


def test_api_error_is_raised_with_status(monkeypatch):
    from src import scanner

    class _Response:
        status_code = 403
        text = "quota exceeded"

        def json(self):
            return {}

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Response())

    with pytest.raises(RuntimeError, match="403"):
        scanner.list_drive_files_api("root", "KEY")


def test_transient_failure_is_retried(monkeypatch):
    """One dropped connection must not abandon a 2700-file listing."""
    import requests

    from src import scanner

    monkeypatch.setattr(scanner.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    class _Response:
        status_code = 200

        def json(self):
            return {"files": [{"id": "f1", "name": "a.mp4",
                               "mimeType": "video/mp4"}]}

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ChunkedEncodingError("Connection broken")
        return _Response()

    monkeypatch.setattr(requests, "get", flaky)

    files = scanner.list_drive_files_api("root", "KEY")

    assert [f["id"] for f in files] == ["f1"]
    assert calls["n"] == 2, "the first attempt must be retried"


def test_bad_key_is_not_retried(monkeypatch):
    """403 means the key or sharing is wrong; retrying only wastes time."""
    import requests

    from src import scanner

    monkeypatch.setattr(scanner.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    class _Response:
        status_code = 403
        text = "API key not valid"

        def json(self):
            return {}

    def denied(*_a, **_k):
        calls["n"] += 1
        return _Response()

    monkeypatch.setattr(requests, "get", denied)

    with pytest.raises(scanner.DriveListingError, match="403"):
        scanner.list_drive_files_api("root", "KEY")
    assert calls["n"] == 1, "a permanent error must not be retried"


# ---- chunked commits --------------------------------------------------------

def _drive_source(session):
    from src.models import Source, SourceKind

    source = Source(name="drive_big", kind=SourceKind.gdrive,
                    location="https://drive.google.com/drive/folders/BIG")
    session.add(source)
    session.flush()
    return source


def _fake_api(count):
    return lambda _fid, _key: [{"id": f"id{i}", "name": f"{i:04}.mp4",
                                "mimeType": "video/mp4"}
                               for i in range(count)]


def test_progress_is_committed_before_the_scan_finishes(session, monkeypatch):
    """The count must move during the run, not appear only at the end.

    A scan that shows 0 rows for ten minutes is indistinguishable from one
    that is silently failing -- which is exactly how the 50-file bug looked.
    """
    from src import scanner
    from src.config import get_settings

    monkeypatch.setattr(scanner, "list_drive_files_api", _fake_api(10))
    monkeypatch.setattr(scanner, "get_settings",
                        lambda: type("S", (), {"google_api_key": "K",
                                               "scan_commit_batch": 4})())
    commits = []
    real_commit = session.commit
    monkeypatch.setattr(session, "commit",
                        lambda: (commits.append(1), real_commit())[1])

    source = _drive_source(session)
    stats = scanner.scan_gdrive_source(session, source, None)

    assert stats["added"] == 10
    assert len(commits) >= 3, \
        f"10 files at batch 4 should commit mid-scan, saw {len(commits)}"


def test_one_commit_per_file_is_not_what_happens(session, monkeypatch):
    """Batching is the point: per-file commits would pay a round trip each."""
    from src import scanner

    monkeypatch.setattr(scanner, "list_drive_files_api", _fake_api(100))
    monkeypatch.setattr(scanner, "get_settings",
                        lambda: type("S", (), {"google_api_key": "K",
                                               "scan_commit_batch": 25})())
    commits = []
    real_commit = session.commit
    monkeypatch.setattr(session, "commit",
                        lambda: (commits.append(1), real_commit())[1])

    scanner.scan_gdrive_source(session, _drive_source(session), None)

    assert len(commits) <= 6, \
        f"100 files at batch 25 must not commit 100 times, saw {len(commits)}"


def test_interrupted_scan_keeps_earlier_batches_and_resumes(session,
                                                            monkeypatch):
    """A crash at file 2700 used to throw away all 2699 before it."""
    from sqlalchemy import func, select

    from src import scanner
    from src.models import Video

    monkeypatch.setattr(scanner, "get_settings",
                        lambda: type("S", (), {"google_api_key": "K",
                                               "scan_commit_batch": 5})())
    monkeypatch.setattr(scanner, "list_drive_files_api", _fake_api(20))
    source = _drive_source(session)

    # Interrupt inside the insert loop, after two batches have committed --
    # a dropped connection or a killed container, not a listing failure.
    calls = {"n": 0}
    real_scalar = session.scalar

    def flaky_scalar(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 13:
            raise ConnectionError("network died mid-scan")
        return real_scalar(*args, **kwargs)

    monkeypatch.setattr(session, "scalar", flaky_scalar)
    with pytest.raises(ConnectionError):
        scanner.scan_gdrive_source(session, source, None)
    monkeypatch.setattr(session, "scalar", real_scalar)
    # session_scope rolls back on the way out, discarding the partial batch.
    # Without this the uncommitted rows are still visible in this same session
    # and the test would pass while proving nothing.
    session.rollback()

    kept = session.scalar(select(func.count()).select_from(Video))
    assert kept == 10, f"committed batches must survive the crash, kept {kept}"

    # The re-run sees the survivors as known and only adds the remainder.
    stats = scanner.scan_gdrive_source(session, source, None)

    assert stats["added"] == 10, "a re-run must not duplicate the first batches"
    assert stats["skipped"] + stats["updated"] == 10
    assert session.scalar(select(func.count()).select_from(Video)) == 20
