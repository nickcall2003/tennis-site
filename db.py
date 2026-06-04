"""
db.py
-----
Database connection. Uses SQLite by default so the demo runs with zero setup.
For production, set DATABASE_URL to your Postgres connection string and nothing
else changes:

    export DATABASE_URL=postgresql+psycopg://user:pass@host:5432/tennis
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tennis.db")

# Neon/Heroku/Render often hand you a URL starting with "postgres://" or
# "postgresql://". SQLAlchemy needs an explicit driver, and Neon needs SSL.
# Normalize automatically so you can paste the raw connection string as-is.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

if DATABASE_URL.startswith("postgresql+psycopg://") and "sslmode=" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

# check_same_thread is only needed for SQLite + the background poller thread.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
if DATABASE_URL.startswith("sqlite"):
    # Let writers wait for a lock instead of instantly erroring ("database is locked").
    connect_args["timeout"] = 30

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True,
                           pool_pre_ping=True)
else:
    # Postgres: size the pool for concurrent requests + background threads, and
    # recycle connections so stale ones don't cause hangs. pool_pre_ping verifies
    # a connection is alive before use.
    engine = create_engine(
        DATABASE_URL, future=True, pool_pre_ping=True,
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
        pool_recycle=1800, pool_timeout=30,
    )

# For SQLite, turn on WAL mode so reads don't block writes (much better
# concurrency for our web-requests + background-poller setup).
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    import models  # noqa: F401  (register models)
    Base.metadata.create_all(engine)
