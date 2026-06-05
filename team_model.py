"""
team_model.py
-------------
Win-probability model for NBA and NFL (team-vs-team sports).

Approach: Elo with home-court/field advantage — the same method 538 used for
both leagues, and it's hard to beat for the effort. We seed each team's rating
from their season win% (so we have sensible numbers early in a season before
Elo has converged), then the live Elo carries the load as games are played.

Two knobs differ by sport:
  - HOME_EDGE: points added to the home team's rating.
      NBA home teams win ~58-60%  -> ~70 Elo points
      NFL home teams win ~55-57%  -> ~50 Elo points
  - rating spread from win% (NFL seasons are short, so win% is noisier).

We also expose an expected-margin estimate for the spread/total module later.
"""

from __future__ import annotations

import math

ELO_BASE = 1500.0

SPORT_CFG = {
    "nba": {"home_edge": 70.0, "winpct_spread": 600.0, "div": 400.0,
            "avg_total": 226.0, "margin_per_elo": 0.030},
    "nfl": {"home_edge": 50.0, "winpct_spread": 500.0, "div": 400.0,
            "avg_total": 44.0, "margin_per_elo": 0.020},
    # College football: big home-field edge and a huge talent spread
    # (blue-bloods vs cupcakes), higher scoring than the NFL.
    "ncaaf": {"home_edge": 65.0, "winpct_spread": 750.0, "div": 400.0,
              "avg_total": 54.0, "margin_per_elo": 0.022},
    # Men's college basketball: strong home court, wide team-quality spread.
    "ncaab": {"home_edge": 75.0, "winpct_spread": 650.0, "div": 400.0,
              "avg_total": 143.0, "margin_per_elo": 0.032},
    # Women's college basketball: similar shape, slightly lower scoring.
    "wncaab": {"home_edge": 72.0, "winpct_spread": 650.0, "div": 400.0,
               "avg_total": 132.0, "margin_per_elo": 0.030},
}


def _winpct_to_rating(win_pct, spread):
    """Map a season win% (0..1) to an Elo-ish rating centered on 1500."""
    if win_pct is None:
        return ELO_BASE
    return ELO_BASE + (win_pct - 0.5) * spread


def expected(rating_a, rating_b, div=400.0):
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / div))


def predict(sport, home_winpct, away_winpct, home_rating=None, away_rating=None):
    """
    Probability the HOME team wins.
    Pass explicit Elo ratings if you track them; otherwise we derive from win%.
    Returns dict with prob_home, expected margin, and a confidence flag.
    """
    cfg = SPORT_CFG.get(sport, SPORT_CFG["nba"])
    rh = home_rating if home_rating is not None else _winpct_to_rating(home_winpct, cfg["winpct_spread"])
    ra = away_rating if away_rating is not None else _winpct_to_rating(away_winpct, cfg["winpct_spread"])
    rh_adj = rh + cfg["home_edge"]
    prob_home = expected(rh_adj, ra, cfg["div"])
    # expected margin (home minus away points) from the rating gap
    margin = (rh_adj - ra) * cfg["margin_per_elo"]

    # confidence: low if we have no record yet for a side
    games_known = (home_winpct is not None and away_winpct is not None)
    conf = "high" if games_known else "low"
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),       # positive => home favored by this many
        "home_rating": round(rh_adj),
        "away_rating": round(ra),
        "confidence": conf,
        "avg_total": cfg["avg_total"],
    }
