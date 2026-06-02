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

# check_same_thread is only needed for SQLite + the background poller thread.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    import models  # noqa: F401  (register models)
    Base.metadata.create_all(engine)
