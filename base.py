"""
odds.py
-------
Everything for turning sportsbook odds into a FAIR probability you can
honestly compare your model against.

This file is where most beginner prediction sites go wrong. The key idea:

  A sportsbook line is NOT a probability. It includes the "vig" (the book's
  built-in margin). The two sides of a market add up to MORE than 100%. You
  must strip that margin out before comparing anything to your model, or
  every game will look like a bet when it isn't.

Example: Celtics -150, opponent +130
  -150 implies 60.0%
  +130 implies 43.5%
  Total = 103.5%   <- that extra 3.5% is the vig
  Fair Celtics = 60.0 / 103.5 = 58.0%   <- THIS is what you compare to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


# ---- American odds <-> probability --------------------------------------

def american_to_prob(odds: int) -> float:
    """Convert American odds to the implied probability (vig still included)."""
    if odds < 0:
        return -odds / (-odds + 100.0)
    return 100.0 / (odds + 100.0)


def american_to_decimal(odds: int) -> float:
    """Convert American odds to decimal odds (total return per $1 staked)."""
    if odds < 0:
        return 1.0 + 100.0 / -odds
    return 1.0 + odds / 100.0


# ---- removing the vig ----------------------------------------------------

def devig_two_way(odds_a: int, odds_b: int) -> tuple[float, float]:
    """
    Take both sides of a two-way market (e.g. a tennis match) and return the
    FAIR, no-vig probabilities that sum to exactly 1.0.

    Uses the simple proportional method: divide each implied probability by
    the total. It's the standard starting point. (Fancier methods exist --
    Shin, logarithmic, power -- but proportional is fine to begin with.)
    """
    p_a = american_to_prob(odds_a)
    p_b = american_to_prob(odds_b)
    total = p_a + p_b
    return p_a / total, p_b / total


# ---- comparing model vs market ------------------------------------------

def edge(model_prob: float, fair_prob: float) -> float:
    """
    Your edge = how much more likely YOUR model thinks the outcome is, versus
    the book's fair (no-vig) probability. Positive = potential value.
    """
    return model_prob - fair_prob


def expected_value(model_prob: float, odds: int) -> float:
    """
    Expected profit per $1 staked, using YOUR model's probability against the
    actual (with-vig) payout the book offers. Positive EV = +EV bet.

        EV = p * (decimal_odds - 1)  -  (1 - p) * 1
    """
    profit_if_win = american_to_decimal(odds) - 1.0
    return model_prob * profit_if_win - (1.0 - model_prob) * 1.0


# ---- closing line value tracking ----------------------------------------
#
# CLV is the real test of whether your model has signal. Win/loss is noisy
# over hundreds of games; CLV tells the truth in dozens. The question it
# answers: did your prediction beat where the line CLOSED (right before the
# match)? If your model's fair probability is consistently higher than the
# closing fair probability on the side you flagged, you are "beating the
# close" -- the strongest evidence a model is real.

@dataclass
class CLVRecord:
    match: str
    side: str                 # which player you flagged
    model_prob: float         # your model's probability for that side
    open_fair_prob: float     # fair (no-vig) prob when you made the pick
    close_fair_prob: float | None = None  # filled in right before the match
    won: bool | None = None   # filled in after the match settles

    @property
    def clv(self) -> float | None:
        """
        Positive CLV means the market moved toward your side after you picked
        it (the closing fair prob rose above where you got in). That's good.
        """
        if self.close_fair_prob is None:
            return None
        return self.close_fair_prob - self.open_fair_prob


@dataclass
class CLVTracker:
    """A tiny in-memory log. In a real site this lives in your database."""

    records: list[CLVRecord] = field(default_factory=list)

    def log(self, match: str, side: str, model_prob: float, open_fair_prob: float) -> CLVRecord:
        rec = CLVRecord(match=match, side=side, model_prob=model_prob, open_fair_prob=open_fair_prob)
        self.records.append(rec)
        return rec

    def summary(self) -> dict:
        closed = [r for r in self.records if r.clv is not None]
        if not closed:
            return {"picks": len(self.records), "closed": 0}
        avg_clv = sum(r.clv for r in closed) / len(closed)
        beat = sum(1 for r in closed if r.clv > 0)
        return {
            "picks": len(self.records),
            "closed": len(closed),
            "avg_clv": avg_clv,
            "pct_beat_close": beat / len(closed),
        }
