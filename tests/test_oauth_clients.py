"""Per-account OAuth app credentials.

Different Google accounts are different Cloud projects, so each YouTube channel
carries its own client id/secret. Refreshing a token against the wrong client
fails with an opaque invalid_client, so the resolution order matters.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.crypto import decrypt, encrypt
from src.models import AccountCredential, Base, Platform
from src.publishers.youtube import YouTubePublisher
from tests.conftest import make_account

AUTH = {"Authorization": "Bearer test-token-123"}


def test_account_client_beats_the_env_fallback(session, monkeypatch):
    account = make_account(session, "yt_one", Platform.youtube)
    session.add(AccountCredential(
        account_id=account.id, client_id="acct-client.apps.googleusercontent.com",
        client_secret_encrypted=encrypt("acct-secret")))
    session.flush()
    session.refresh(account)

    publisher = YouTubePublisher()
    publisher.settings.youtube_client_id = "env-client"
    publisher.settings.youtube_client_secret = "env-secret"

    client_id, secret = publisher.oauth_client(account)
    assert client_id == "acct-client.apps.googleusercontent.com"
    assert secret == "acct-secret"


def test_env_is_used_when_the_account_has_none(session):
    account = make_account(session, "yt_two", Platform.youtube)
    publisher = YouTubePublisher()
    publisher.settings.youtube_client_id = "env-client"
    publisher.settings.youtube_client_secret = "env-secret"

    assert publisher.oauth_client(account) == ("env-client", "env-secret")


def test_two_channels_can_use_different_projects(session):
    """The whole point: separate Google accounts, separate clients."""
    first = make_account(session, "channel_a", Platform.youtube)
    second = make_account(session, "channel_b", Platform.youtube)
    session.add(AccountCredential(account_id=first.id, client_id="project-a",
                                  client_secret_encrypted=encrypt("secret-a")))
    session.add(AccountCredential(account_id=second.id, client_id="project-b",
                                  client_secret_encrypted=encrypt("secret-b")))
    session.flush()
    session.refresh(first)
    session.refresh(second)

    publisher = YouTubePublisher()
    assert publisher.oauth_client(first) == ("project-a", "secret-a")
    assert publisher.oauth_client(second) == ("project-b", "secret-b")


def test_missing_client_raises_a_useful_error(session):
    from src.publishers.base import PublishError

    account = make_account(session, "yt_three", Platform.youtube)
    publisher = YouTubePublisher()
    publisher.settings.youtube_client_id = ""
    publisher.settings.youtube_client_secret = ""

    with pytest.raises(PublishError, match="set-oauth-client"):
        publisher.oauth_client(account)


def test_secret_is_encrypted_at_rest(session):
    account = make_account(session, "yt_four", Platform.youtube)
    credential = AccountCredential(account_id=account.id, client_id="cid",
                                   client_secret_encrypted=encrypt("plaintext"))
    session.add(credential)
    session.flush()

    assert "plaintext" not in credential.client_secret_encrypted
    assert credential.client_secret_encrypted.startswith("enc:v1:")
    assert decrypt(credential.client_secret_encrypted) == "plaintext"


# ---- API ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from contextlib import contextmanager

    os.environ["ADMIN_API_TOKEN"] = "test-token-123"
    from src import api as api_module

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


def test_api_stores_a_client_without_echoing_the_secret(client):
    client.post("/accounts", json={"platform": "youtube", "username": "yt",
                                   "account_name": "YT"}, headers=AUTH)
    response = client.post("/accounts/yt/oauth-client",
                           json={"client_id": "abc.apps.googleusercontent.com",
                                 "client_secret": "super-secret"}, headers=AUTH)
    assert response.status_code == 200
    assert "super-secret" not in response.text, "the secret must never be echoed"


def test_mapping_reports_presence_not_secrets(client):
    client.post("/accounts", json={"platform": "youtube", "username": "yt",
                                   "account_name": "YT"}, headers=AUTH)
    client.post("/accounts/yt/oauth-client",
                json={"client_id": "abc", "client_secret": "super-secret"},
                headers=AUTH)

    body = client.get("/mapping", headers=AUTH).text
    assert "super-secret" not in body
    row = next(r for r in client.get("/mapping", headers=AUTH).json()
               if r["username"] == "yt")
    assert row["oauth_client_id"] == "abc"
    assert row["has_client_secret"] is True
    assert row["has_refresh_token"] is False


def test_bulk_accepts_per_account_oauth_clients(client):
    payload = {
        "accounts": [
            {"platform": "youtube", "username": "yt_a", "account_name": "A"},
            {"platform": "youtube", "username": "yt_b", "account_name": "B"},
        ],
        "oauth_clients": {
            "yt_a": {"client_id": "project-a", "client_secret": "sa"},
            "yt_b": {"client_id": "project-b", "client_secret": "sb"},
        },
    }
    response = client.post("/bulk", json=payload, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["oauth_clients"] == 2

    rows = {r["username"]: r for r in client.get("/mapping", headers=AUTH).json()}
    assert rows["yt_a"]["oauth_client_id"] == "project-a"
    assert rows["yt_b"]["oauth_client_id"] == "project-b"
