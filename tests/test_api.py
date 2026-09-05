"""Admin mapping API: auth, and the bulk wiring path."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["ADMIN_API_TOKEN"] = "test-token-123"

from src import api as api_module  # noqa: E402
from src.models import Base  # noqa: E402

AUTH = {"Authorization": "Bearer test-token-123"}


@pytest.fixture
def client(monkeypatch):
    """Point the API's session_scope at an in-memory database."""
    from contextlib import contextmanager

    # StaticPool keeps every session on one connection: a plain in-memory
    # SQLite engine hands each new connection its own empty database.
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    monkeypatch.setattr(api_module, "session_scope", scope)
    api_module.get_settings.cache_clear()
    return TestClient(api_module.app)


def test_health_needs_no_token(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_requests_without_a_token_are_rejected(client):
    response = client.get("/mapping")
    assert response.status_code == 401


def test_wrong_token_is_rejected(client):
    response = client.get("/mapping", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_bulk_wires_accounts_to_their_own_links(client):
    """The whole routing table in one POST."""
    payload = {
        "sources": [
            {"name": "link1", "location": "channel_one", "kind": "local"},
            {"name": "link2", "location": "channel_two", "kind": "local"},
            {"name": "link3", "location": "https://cdn.example.com/x",
             "kind": "remote_url"},
        ],
        "accounts": [
            {"platform": "youtube", "username": "youtube_1",
             "account_name": "YT One"},
            {"platform": "youtube", "username": "youtube_2",
             "account_name": "YT Two"},
            {"platform": "instagram", "username": "instagram_1",
             "account_name": "IG One"},
            {"platform": "instagram", "username": "instagram_2",
             "account_name": "IG Two"},
        ],
        "mappings": [
            {"account": "youtube_1", "sources": ["link1"],
             "themes": {"Motivation": 10},
             "schedules": [{"publish_time": "10:00", "videos_per_day": 1}]},
            {"account": "youtube_2", "sources": ["link2"],
             "themes": {"Motivation": 10}},
            {"account": "instagram_1", "sources": ["link2"],
             "themes": {"Gym": 10}},
            {"account": "instagram_2", "sources": ["link3"],
             "themes": {"Supercars": 10}},
        ],
    }
    response = client.post("/bulk", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {"sources": 3, "accounts": 4, "mappings": 4}

    mapping = client.get("/mapping", headers=AUTH).json()
    routed = {row["username"]: row["sources"] for row in mapping}
    assert routed == {"youtube_1": ["link1"], "youtube_2": ["link2"],
                      "instagram_1": ["link2"], "instagram_2": ["link3"]}


def test_remapping_sources_replaces_rather_than_appends(client):
    client.post("/sources", json={"name": "a", "location": "fa"}, headers=AUTH)
    client.post("/sources", json={"name": "b", "location": "fb"}, headers=AUTH)
    client.post("/accounts", json={"platform": "youtube", "username": "yt",
                                   "account_name": "YT"}, headers=AUTH)

    client.post("/accounts/yt/sources", json=["a"], headers=AUTH)
    client.post("/accounts/yt/sources", json=["b"], headers=AUTH)

    row = next(r for r in client.get("/mapping", headers=AUTH).json()
               if r["username"] == "yt")
    assert row["sources"] == ["b"], "re-mapping must not accumulate old links"


def test_unknown_account_is_a_404(client):
    client.post("/sources", json={"name": "a", "location": "fa"}, headers=AUTH)
    response = client.post("/accounts/ghost/sources", json=["a"], headers=AUTH)
    assert response.status_code == 404


# ---- /ingest ---------------------------------------------------------------

def _account_for_ingest(client):
    client.post("/accounts", json={"platform": "youtube", "username": "yt",
                                   "account_name": "YT"}, headers=AUTH)


class _FakeJob:
    id = "job-1"


class _FakeQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, *args, **kwargs):
        self.jobs.append(args)
        return _FakeJob()


@pytest.fixture
def fake_queue(monkeypatch):
    """/ingest imports get_queue inside the handler, so the patch has to land
    on src.worker - patching src.api would never be consulted."""
    import src.worker as worker_module

    queue = _FakeQueue()
    monkeypatch.setattr(worker_module, "get_queue", lambda: queue)
    return queue


def test_ingest_wires_a_registered_theme(client, fake_queue):
    """One call wires an existing theme end to end."""
    _account_for_ingest(client)
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)

    response = client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/ABC"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["theme"] == "Gym"
    assert body["source_created"] is True
    assert body["source"] == "drive_gym"
    assert body["indexing"] == "queued"
    assert [a["username"] for a in body["accounts"]] == ["yt"]
    assert fake_queue.jobs, "indexing must be queued, never run inline"

    row = next(r for r in client.get("/mapping", headers=AUTH).json()
               if r["username"] == "yt")
    assert "Gym" in row["themes"]
    assert "drive_gym" in row["sources"]


def test_ingest_rejects_an_unregistered_theme(client, fake_queue):
    """A typo must fail loudly, not silently create a theme nobody publishes."""
    _account_for_ingest(client)
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)

    response = client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Gymm",
        "drive_url": "https://drive.google.com/drive/folders/ABC"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "unknown theme" in detail["error"]
    assert "Gym" in detail["valid_themes"], "the error must list what is allowed"
    assert not fake_queue.jobs, "nothing may be indexed for an unknown theme"


def test_theme_registry_lists_and_rejects_duplicates(client):
    assert client.get("/themes", headers=AUTH).json() == []

    created = client.post("/themes", json={"name": "Gym"}, headers=AUTH)
    assert created.status_code == 200 and created.json()["created"] is True

    duplicate = client.post("/themes", json={"name": "Gym"}, headers=AUTH)
    assert duplicate.status_code == 409, "themes must be unique"

    names = [t["name"] for t in client.get("/themes", headers=AUTH).json()]
    assert names == ["Gym"]


def test_ingest_reuses_an_existing_theme(client, fake_queue):
    """Re-ingesting the same theme repoints the source rather than duplicating."""
    _account_for_ingest(client)
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)

    first = client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/ABC"}).json()
    second = client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/DEF"}).json()

    assert first["source_created"] is True
    assert second["source_created"] is False, "same source name must be reused"
    sources = client.get("/sources", headers=AUTH).json()
    assert len(sources) == 1, "no duplicate source rows"
    assert sources[0]["location"].endswith("DEF"), "URL must be repointed"


def test_ingest_adds_a_second_source_without_dropping_the_first(client, fake_queue):
    _account_for_ingest(client)
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)
    client.post("/themes", json={"name": "Cars"}, headers=AUTH)
    client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/A"})
    client.post("/ingest", headers=AUTH, json={
        "account": "yt", "theme": "Cars",
        "drive_url": "https://drive.google.com/drive/folders/B"})

    row = next(r for r in client.get("/mapping", headers=AUTH).json()
               if r["username"] == "yt")
    assert set(row["sources"]) == {"drive_gym", "drive_cars"}
    assert set(row["themes"]) == {"Gym", "Cars"}


def test_ingest_rejects_an_unknown_account(client):
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)
    response = client.post("/ingest", headers=AUTH, json={
        "account": "ghost", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/A"})
    assert response.status_code == 404


def test_account_can_be_addressed_by_id_or_name(client, fake_queue):
    """Ids are stable across renames; names are not."""
    created = client.post("/accounts",
                          json={"platform": "youtube", "username": "old_name",
                                "account_name": "Old Name"}, headers=AUTH).json()
    client.post("/themes", json={"name": "Gym"}, headers=AUTH)
    account_id = created["id"]

    by_name = client.post("/ingest", headers=AUTH, json={
        "account": "old_name", "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/A"})
    assert by_name.status_code == 200

    by_id = client.post("/ingest", headers=AUTH, json={
        "account": str(account_id), "theme": "Gym",
        "drive_url": "https://drive.google.com/drive/folders/A"})
    assert by_id.status_code == 200
    assert by_id.json()["accounts"][0]["username"] == "old_name"


def test_ingest_accepts_a_list_of_accounts(client, fake_queue):
    """One theme + one Drive folder wired to several accounts in a single call."""
    for name in ("yt_one", "ig_one", "ig_two"):
        platform = "youtube" if name.startswith("yt") else "instagram"
        client.post("/accounts", json={"platform": platform, "username": name,
                                       "account_name": name}, headers=AUTH)
    client.post("/themes", json={"name": "Luxury", "coordinated": True},
                headers=AUTH)

    response = client.post("/ingest", headers=AUTH, json={
        "account": ["yt_one", "ig_one", "ig_two"],
        "theme": "Luxury",
        "drive_url": "https://drive.google.com/drive/folders/ABC"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert [a["username"] for a in body["accounts"]] == ["yt_one", "ig_one", "ig_two"]
    assert all(a["theme_mapped"] for a in body["accounts"])
    assert all(a["source_mapped"] for a in body["accounts"])
    assert body["coordinated"] is True
    assert len(fake_queue.jobs) == 1, "the folder is indexed once, not per account"

    mapping = {r["username"]: r for r in client.get("/mapping", headers=AUTH).json()}
    for name in ("yt_one", "ig_one", "ig_two"):
        assert "Luxury" in mapping[name]["themes"]
        assert "drive_luxury" in mapping[name]["sources"]


def test_ingest_list_is_all_or_nothing_on_a_bad_account(client, fake_queue):
    """A typo in the third entry must not half-wire the first two."""
    client.post("/accounts", json={"platform": "youtube", "username": "good",
                                   "account_name": "good"}, headers=AUTH)
    client.post("/themes", json={"name": "Luxury"}, headers=AUTH)

    response = client.post("/ingest", headers=AUTH, json={
        "account": ["good", "ghost"], "theme": "Luxury",
        "drive_url": "https://drive.google.com/drive/folders/ABC"})
    assert response.status_code == 404

    row = next(r for r in client.get("/mapping", headers=AUTH).json()
               if r["username"] == "good")
    assert row["themes"] == {}, "nothing may be wired when one account is unknown"
    assert not fake_queue.jobs, "nothing indexed either"


def test_ingest_still_accepts_a_single_string(client, fake_queue):
    client.post("/accounts", json={"platform": "youtube", "username": "solo",
                                   "account_name": "solo"}, headers=AUTH)
    client.post("/themes", json={"name": "Luxury"}, headers=AUTH)
    response = client.post("/ingest", headers=AUTH, json={
        "account": "solo", "theme": "Luxury",
        "drive_url": "https://drive.google.com/drive/folders/ABC"})
    assert response.status_code == 200
    assert len(response.json()["accounts"]) == 1
