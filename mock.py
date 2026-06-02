"""
providers/mock.py
-----------------
A fake data feed that SIMULATES live tennis so the whole system runs today
with zero cost and no API keys. Each call to get_live_score advances the
match by one point, so as the poller ticks you see real score progression:
"0-0" -> "15-0" -> "30-0" ... games, sets, and eventually a winner.

Each match is seeded with a per-point "player A wins this point" probability
derived from your Elo model, so the simulated results actually track the
predictions. ITF matches return NO stats, to exercise the "stats unavailable"
path in the UI exactly as real ITF data would.

Swap this out for a real provider (see goalserve.py) without touching anything
else.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from base import (
    LiveScore,
    MatchInfo,
    MatchStats,
    PlayerStats,
    TennisProvider,
)

# Tennis point ladder.
_POINTS = ["0", "15", "30", "40"]


class _MatchSim:
    """Holds and advances the state of one simulated match."""

    def __init__(self, info: MatchInfo, p_a_point: float, seed: int):
        self.info = info
        self.p_a_point = p_a_point          # prob A wins a given point
        self.rng = random.Random(seed)
        self.sets_a: list[int] = [0]
        self.sets_b: list[int] = [0]
        self.pt_a = 0                       # index into _POINTS, or "AD" handled below
        self.pt_b = 0
        self.adv: str | None = None         # "a" | "b" | None (deuce advantage)
        self.server = "a"
        self.status = "scheduled"
        self.winner: str | None = None
        self.sets_to_win = 2 if info.best_of == 3 else 3
        # crude running stat counters (only used for non-ITF tiers)
        self.points_a = 0
        self.points_b = 0
        self.aces_a = self.aces_b = 0
        self.df_a = self.df_b = 0

    # ---- one point -------------------------------------------------------

    def advance_point(self) -> None:
        if self.status == "finished":
            return
        self.status = "live"

        a_wins = self.rng.random() < self.p_a_point
        if a_wins:
            self.points_a += 1
            if self.server == "a" and self.rng.random() < 0.06:
                self.aces_a += 1
        else:
            self.points_b += 1
            if self.server == "a" and self.rng.random() < 0.04:
                self.df_a += 1

        self._award_point("a" if a_wins else "b")

    def _award_point(self, who: str) -> None:
        # Deuce / advantage logic.
        if self.pt_a >= 3 and self.pt_b >= 3:
            if self.adv is None:
                self.adv = who
            elif self.adv == who:
                self._win_game(who)
            else:
                self.adv = None  # back to deuce
            return
        if who == "a":
            self.pt_a += 1
        else:
            self.pt_b += 1
        if self.pt_a == 4:
            self._win_game("a")
        elif self.pt_b == 4:
            self._win_game("b")

    def _win_game(self, who: str) -> None:
        self.pt_a = self.pt_b = 0
        self.adv = None
        if who == "a":
            self.sets_a[-1] += 1
        else:
            self.sets_b[-1] += 1
        self.server = "b" if self.server == "a" else "a"
        self._check_set()

    def _play_tiebreak(self) -> str:
        """First to 7 points, win by 2, weighted by the per-point edge."""
        pa = pb = 0
        while True:
            if self.rng.random() < self.p_a_point:
                pa += 1
            else:
                pb += 1
            if (pa >= 7 or pb >= 7) and abs(pa - pb) >= 2:
                return "a" if pa > pb else "b"

    def _check_set(self) -> None:
        a, b = self.sets_a[-1], self.sets_b[-1]

        set_won_by: str | None = None
        if a == 6 and b == 6:                       # tiebreak decides the set
            tb = self._play_tiebreak()
            if tb == "a":
                self.sets_a[-1] = 7
            else:
                self.sets_b[-1] = 7
            set_won_by = tb
        elif a >= 6 and a - b >= 2:
            set_won_by = "a"
        elif b >= 6 and b - a >= 2:
            set_won_by = "b"

        if set_won_by is None:
            return

        sets_won_a = sum(1 for x, y in zip(self.sets_a, self.sets_b) if x > y)
        sets_won_b = sum(1 for x, y in zip(self.sets_a, self.sets_b) if y > x)
        if sets_won_a == self.sets_to_win:
            self._finish("a")
        elif sets_won_b == self.sets_to_win:
            self._finish("b")
        else:
            self.sets_a.append(0)  # next set
            self.sets_b.append(0)

    def _finish(self, who: str) -> None:
        self.status = "finished"
        self.winner = who

    # ---- views -----------------------------------------------------------

    def _game_label(self, side: str) -> str:
        if self.pt_a >= 3 and self.pt_b >= 3:
            if self.adv == side:
                return "AD"
            if self.adv is not None:
                return "40"
            return "40"
        return _POINTS[self.pt_a if side == "a" else self.pt_b]

    def live_score(self) -> LiveScore:
        return LiveScore(
            sets_a=list(self.sets_a),
            sets_b=list(self.sets_b),
            game_a=self._game_label("a"),
            game_b=self._game_label("b"),
            server=self.server,
            status=self.status,
            winner=self.winner,
        )

    def stats(self) -> MatchStats:
        # ITF: stats not collected at this level. Return empty, like real data.
        if self.info.tier == "ITF":
            return MatchStats()
        tot = max(1, self.points_a + self.points_b)

        def side(points, aces, df) -> PlayerStats:
            return PlayerStats(
                aces=aces,
                double_faults=df,
                first_serve_pct=0.55 + 0.1 * self.rng.random(),
                first_serve_won_pct=0.65 + 0.1 * self.rng.random(),
                second_serve_won_pct=0.45 + 0.1 * self.rng.random(),
                break_points_won=int(points * 0.05),
                break_points_faced=int(points * 0.08),
                total_points_won=points,
            )

        return MatchStats(
            player_a=side(self.points_a, self.aces_a, self.df_a),
            player_b=side(self.points_b, self.aces_b, self.df_b),
        )


class MockTennisProvider(TennisProvider):
    name = "mock"

    def __init__(self) -> None:
        self._sims: dict[str, _MatchSim] = {}
        self._schedule: list[MatchInfo] = []

    def seed_matches(self, matches: list[tuple[MatchInfo, float]]) -> None:
        """matches: list of (MatchInfo, prob_a_wins_match). Called by seed.py."""
        self._schedule = [m for m, _ in matches]
        for i, (info, p_match) in enumerate(matches):
            # Convert a match-win probability into a per-point edge. A small
            # per-point edge compounds into a large match edge, so we scale it.
            p_point = 0.5 + (p_match - 0.5) * 0.18
            p_point = min(0.62, max(0.38, p_point))
            self._sims[info.provider_match_id] = _MatchSim(info, p_point, seed=100 + i)

    # ---- TennisProvider contract ----------------------------------------

    def get_schedule(self, day: datetime) -> list[MatchInfo]:
        return list(self._schedule)

    def get_live_score(self, provider_match_id: str) -> LiveScore:
        sim = self._sims[provider_match_id]
        sim.advance_point()              # mock "play" advances on each poll
        info = sim.info
        info.status = sim.status         # keep MatchInfo status in sync
        return sim.live_score()

    def get_match_stats(self, provider_match_id: str) -> MatchStats:
        return self._sims[provider_match_id].stats()


def demo_schedule(now: datetime | None = None) -> list[MatchInfo]:
    """A handful of matches across all four tiers for the demo."""
    now = now or datetime.now()
    base = now.replace(minute=0, second=0, microsecond=0)
    rows = [
        ("ATP",        "Madrid Open",          "Clay",  "Carlos Alcaraz",  "Casper Ruud",        0),
        ("ATP",        "Madrid Open",          "Clay",  "Jannik Sinner",   "Daniil Medvedev",    1),
        ("WTA",        "Madrid Open",          "Clay",  "Iga Swiatek",     "Aryna Sabalenka",    0),
        ("CHALLENGER", "Prague Challenger",    "Clay",  "Jakub Mensik",    "Dalibor Svrcina",    2),
        ("ITF",        "ITF M25 Antalya",      "Hard",  "Local Qualifier", "Wildcard Entry",     0),
    ]
    out = []
    for i, (tier, tourn, surf, a, b, hours) in enumerate(rows):
        out.append(
            MatchInfo(
                provider_match_id=f"mock-{i}",
                tier=tier,
                tournament=tourn,
                surface=surf,
                player_a=a,
                player_b=b,
                scheduled=base + timedelta(hours=hours),
                best_of=3,
                status="scheduled",
            )
        )
    return out
