"""
db.py
-----
Database setup. Two modes, chosen automatically:

  1. If DATABASE_URL is set (e.g. you re-attach a Railway Postgres plugin),
     we use it. Postgres persists on its own.

  2. Otherwise we use SQLite, and the file lives at DB_PATH. On Railway the
     filesystem is WIPED on every restart/redeploy unless the file is inside a
     mounted Volume. So DB_PATH must point inside your volume mount (e.g.
     /data/linelogic.db). If it doesn't, your accuracy log, pick history and
     built tennis slates are erased on every restart — which is what caused the
     rebuild-on-every-boot loop.

IMPORTANT: make sure you do NOT have a stale DATABASE_URL variable left over
from the old Postgres plugin. If one is set and points at a dead database, the
app will try Postgres and fail to start. Delete that variable to use SQLite.
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
    print("[db] using DATABASE_URL from environment")

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
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

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
