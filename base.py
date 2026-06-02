"""
providers/base.py
-----------------
The seam between your site and whatever data feed you buy.

Every provider (Goalserve, Matchstat, etc.) returns data in its own shape.
Rather than letting that shape leak into the rest of the app, each provider
gets an *adapter* that translates the feed into these neutral dataclasses.
The database, prediction engine, API, and UI only ever see THESE types -- so
you can swap or add providers later without touching anything else.

To add a real provider you subclass TennisProvider and implement three
methods. That's the whole contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


# Tiers we support. Predictions work for all four; live STATS are reliable for
# ATP/WTA/CHALLENGER and frequently absent at ITF (see notes in the README).
TIERS = ("ATP", "WTA", "CHALLENGER", "ITF")


@dataclass
class MatchInfo:
    """Pre-match facts: who, where, when, what level."""
    provider_match_id: str
    tier: str                      # one of TIERS
    tournament: str
    surface: str                   # "Hard" | "Clay" | "Grass"
    player_a: str
    player_b: str
    scheduled: datetime
    best_of: int = 3               # 3 for most, 5 for men's Slams
    status: str = "scheduled"      # scheduled | live | finished


@dataclass
class LiveScore:
    """
    A snapshot of where a match stands right now.

    sets_a / sets_b   -> games won in each completed/in-progress set, e.g.
                         [6, 3, 2] means 6-x, 3-x, currently 2-x.
    game_a / game_b   -> current game score as displayed: "0","15","30","40","AD"
    server            -> "a" or "b": who is serving
    status            -> scheduled | live | finished
    winner            -> "a" | "b" | None
    """
    sets_a: list[int] = field(default_factory=list)
    sets_b: list[int] = field(default_factory=list)
    game_a: str = "0"
    game_b: str = "0"
    server: str = "a"
    status: str = "live"
    winner: str | None = None

    def scoreline(self, name_a: str, name_b: str) -> str:
        """Human string like: 'Alcaraz leads 2-0 sets, 3-3, 40-30'."""
        sets = " ".join(f"{a}-{b}" for a, b in zip(self.sets_a, self.sets_b))
        return f"{name_a} {sets} | game {self.game_a}-{self.game_b} (serv: {self.server})"


@dataclass
class PlayerStats:
    """
    In-match serve/return stats for ONE player. Every field is optional
    because at ITF level these often simply aren't collected -- a None here
    means 'not available', which the UI shows gracefully.
    """
    aces: int | None = None
    double_faults: int | None = None
    first_serve_pct: float | None = None          # 0-1
    first_serve_won_pct: float | None = None       # 0-1
    second_serve_won_pct: float | None = None      # 0-1
    break_points_won: int | None = None
    break_points_faced: int | None = None
    total_points_won: int | None = None


@dataclass
class MatchStats:
    """Both players' stats, or None for either side if unavailable."""
    player_a: PlayerStats | None = None
    player_b: PlayerStats | None = None

    @property
    def available(self) -> bool:
        return self.player_a is not None or self.player_b is not None


class TennisProvider(ABC):
    """Implement these three methods for any real data feed."""

    name: str = "base"

    @abstractmethod
    def get_schedule(self, day: datetime) -> list[MatchInfo]:
        """All matches scheduled for the given day, across the tiers you cover."""

    @abstractmethod
    def get_live_score(self, provider_match_id: str) -> LiveScore:
        """Current score state for one match."""

    @abstractmethod
    def get_match_stats(self, provider_match_id: str) -> MatchStats:
        """Current in-match stats for one match (may be empty at ITF level)."""
