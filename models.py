"""
models.py
---------
The database schema. Five tables that mirror the data flow:

  Match        - one row per scheduled match (who/where/when/tier)
  Prediction   - the model's pre-match probability + value vs the book
  LiveState    - the current score for a live match (one row per match)
  StatSnapshot - periodic stat readings during a match (history of the panel)

This is deliberately small. It's the backbone you grow, not the finished thing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_match_id: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(16), index=True)       # ATP/WTA/CHALLENGER/ITF
    tournament: Mapped[str] = mapped_column(String(128))
    surface: Mapped[str] = mapped_column(String(16))
    player_a: Mapped[str] = mapped_column(String(96))
    player_b: Mapped[str] = mapped_column(String(96))
    scheduled: Mapped[datetime] = mapped_column(DateTime, index=True)
    best_of: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(16), default="scheduled", index=True)
    # new fields for the richer UI
    event_time: Mapped[str | None] = mapped_column(String(8), nullable=True)      # "13:30" CT
    tournament_key: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    round: Mapped[str | None] = mapped_column(String(48), nullable=True)
    player_a_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    player_b_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prominence: Mapped[float] = mapped_column(Float, default=0.0, index=True)     # for "biggest matches"

    prediction: Mapped["Prediction"] = relationship(back_populates="match", uselist=False)
    live: Mapped["LiveState"] = relationship(back_populates="match", uselist=False)

    __table_args__ = (UniqueConstraint("provider_match_id", name="uq_provider_match"),)


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    prob_a: Mapped[float] = mapped_column(Float)            # model: P(player_a wins)
    fair_prob_a: Mapped[float | None] = mapped_column(Float, nullable=True)   # de-vigged book
    edge_a: Mapped[float | None] = mapped_column(Float, nullable=True)        # model - fair
    confident: Mapped[bool] = mapped_column(default=True)   # legacy flag
    confidence: Mapped[str] = mapped_column(String(8), default="high")  # high|medium|low
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    match: Mapped["Match"] = relationship(back_populates="prediction")


class LiveState(Base):
    __tablename__ = "live_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True, unique=True)
    sets_a: Mapped[str] = mapped_column(String(32), default="")   # "6,3,2"
    sets_b: Mapped[str] = mapped_column(String(32), default="")
    game_a: Mapped[str] = mapped_column(String(4), default="0")
    game_b: Mapped[str] = mapped_column(String(4), default="0")
    server: Mapped[str] = mapped_column(String(1), default="a")
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    winner: Mapped[str | None] = mapped_column(String(1), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    match: Mapped["Match"] = relationship(back_populates="live")


class StatSnapshot(Base):
    __tablename__ = "stat_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # store the whole stats payload as JSON text; small and flexible.
    payload: Mapped[str] = mapped_column(Text)


class MatchAnalysis(Base):
    """The auto-generated writeup + the weather summary it was based on."""
    __tablename__ = "match_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True, unique=True)
    writeup: Mapped[str] = mapped_column(Text)
    weather: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
