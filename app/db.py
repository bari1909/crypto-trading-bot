from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

_url = settings.database_url
if _url.startswith("sqlite"):
    engine = create_engine(_url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
else:
    engine = create_engine(_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_database():
    """Create the PostgreSQL database named in DATABASE_URL if missing (e.g. the separate futures ledger)."""
    if engine.dialect.name != "postgresql":
        return
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ProgrammingError
    url = make_url(_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar()
            if not exists:
                try:
                    conn.execute(text(f'CREATE DATABASE "{url.database}"'))
                except ProgrammingError:
                    pass   # created concurrently by another service
    finally:
        admin.dispose()


def init_db():
    from sqlalchemy import inspect

    from app import models  # noqa: F401
    ensure_database()
    Base.metadata.create_all(bind=engine)
    # v1 -> v2: bot_state gained heartbeat_at (old v1 tables trades/daily_stats are left untouched).
    cols = {c["name"] for c in inspect(engine).get_columns("bot_state")}
    if "heartbeat_at" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE bot_state ADD COLUMN heartbeat_at TIMESTAMP WITH TIME ZONE"))
    # v2.1: futures support
    pcols = {c["name"] for c in inspect(engine).get_columns("positions")}
    for name, ddl in (("market", "VARCHAR(10) DEFAULT 'spot' NOT NULL"), ("side", "VARCHAR(5) DEFAULT 'long' NOT NULL"),
                      ("funding", "FLOAT DEFAULT 0 NOT NULL")):
        if name not in pcols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE positions ADD COLUMN {name} {ddl}"))


def acquire_worker_lock():
    """Single-worker guarantee. PostgreSQL advisory lock; no-op on SQLite (tests/dev)."""
    if engine.dialect.name != "postgresql":
        return None
    conn = engine.connect()
    if not conn.execute(text("SELECT pg_try_advisory_lock(7319921)")).scalar():
        conn.close()
        raise RuntimeError("Another worker already holds the trading lock")
    return conn
