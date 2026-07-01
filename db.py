"""
db.py
-----
Database setup. Two modes, chosen automatically:

  1. If DATABASE_URL is set (e.g. a Neon/Supabase/Railway Postgres), we use it.
     Postgres persists on its own — this is what you want for real, rolling
     30-day stats that survive redeploys.

  2. Otherwise we use SQLite, and the file lives at DB_PATH. On Railway the
     filesystem is WIPED on every restart/redeploy unless the file is inside a
     mounted Volume. So DB_PATH must point inside your volume mount (e.g.
     /data/linelogic.db). If it doesn't, your accuracy log, pick history and
     built tennis slates are erased on every restart.

For Postgres on a POOLED endpoint (Neon's `-pooler` host runs PgBouncer in
transaction mode) we:
  * pool_pre_ping        -> re-validate a connection before use, so Neon's
                            free-tier auto-suspend doesn't surface as a random
                            "connection closed" error on the first request back.
  * pool_recycle=300     -> drop connections older than 5 min.
  * prepare_threshold=None -> disable psycopg3 server-side prepared statements,
                            which otherwise collide across pooled backends in
                            transaction mode ("prepared statement ... does not
                            exist"). Only applied for the psycopg (v3) driver.

IMPORTANT: with psycopg3 installed (psycopg[binary]), DATABASE_URL must use the
`postgresql+psycopg://` scheme. A bare `postgresql://` makes SQLAlchemy reach
for psycopg2 (not installed) and the app will fail to start.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    # SQLite on a persistent path. Default assumes a Railway Volume at /data.
    db_path = os.environ.get("DB_PATH", "/data/linelogic.db")
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    DATABASE_URL = f"sqlite:///{db_path}"
    print(f"[db] using SQLite at {db_path}")
else:
    # Accept a bare postgres URL and normalize it to the installed driver
    # (psycopg v3) so a plain `postgresql://` from Neon's dashboard still works.
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[db] using DATABASE_URL from environment (host: {safe.split('/')[0]})")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        DATABASE_URL,
        # Required: the live engine + request threadpool both touch the DB from
        # different threads.
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):
        # WAL lets the background live engine write while page requests read,
        # which kills almost all "database is locked" errors on one instance.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
else:
    # Postgres (Neon/Supabase/Render/Railway). Hardened for a pooled endpoint.
    connect_args = {}
    if "+psycopg" in DATABASE_URL:                 # psycopg v3 only
        connect_args["prepare_threshold"] = None   # disable prepared stmts (PgBouncer-safe)
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args=connect_args,
    )

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

# If your models.py says `from db import Base`, leave this here. If models.py
# defines its OWN Base, that's fine too — init_db() below handles both.
Base = declarative_base()


def init_db() -> None:
    """Create all tables.

    models.py defines its OWN Base, so the tables live on that Base's metadata,
    not on the Base in this file. Rather than guess the variable name, we pull
    the shared MetaData directly off the real model classes — that registry
    contains every table regardless of what the Base is called.
    """
    import models  # noqa: F401  (defines + registers the model classes)
    metadatas = set()
    for cls_name in ("Match", "Prediction", "LiveState", "StatSnapshot",
                     "PickResult", "PickLog", "OddsSnapshot"):
        cls = getattr(models, cls_name, None)
        md = getattr(cls, "metadata", None) if cls is not None else None
        if md is not None:
            metadatas.add(md)
    mb = getattr(models, "Base", None)
    if mb is not None and getattr(mb, "metadata", None) is not None:
        metadatas.add(mb.metadata)
    if not metadatas:                 # last-ditch fallback
        metadatas.add(Base.metadata)
    for md in metadatas:
        md.create_all(bind=engine)    # idempotent: safe to run on every boot

    # Lightweight forward migrations. create_all never ADDs columns to a table
    # that already exists, so for columns added after a table shipped we issue a
    # plain ALTER TABLE. Each runs in its OWN transaction so a "duplicate column"
    # (already added on a previous boot) can't poison the others.
    _added_columns = (
        ("golf_matchup_picks", "edge", "REAL"),
        ("pick_log", "prob", "REAL"),
        ("pick_results", "prob", "REAL"),
        ("pick_results", "subcat", "TEXT"),
        ("odds_snapshot", "prob", "REAL"),
        ("odds_snapshot", "subcat", "TEXT"),
    )
    try:
        from sqlalchemy import text
        for table, col, coltype in _added_columns:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                print(f"[init_db] added column {table}.{col}")
            except Exception:
                pass  # already exists (expected after first boot) or table absent
    except Exception as e:
        print(f"[init_db] column migration skipped: {e}")
