"""Engine, session factory and the locking helpers the scheduler depends on."""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_settings().sqlalchemy_url,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
            future=True,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False,
                                     future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Session:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def advisory_lock(session: Session, key: int):
    """Serialise scheduling per account across every scheduler replica.

    Two schedulers ticking at the same second would otherwise both reserve a
    video for the same slot; the row lock alone cannot prevent that because they
    would pick different rows.
    """
    is_postgres = session.bind.dialect.name == "postgresql"
    if is_postgres:
        session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
    try:
        yield
    finally:
        if is_postgres:
            session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
