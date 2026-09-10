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

MARGIN SCALE (margin_per_elo): converts an Elo-rating gap into an expected point
spread. The well-established 538 conversion is ~25 Elo points per 1 point of NFL
spread (i.e. 1/25 = 0.040). The previous values here were roughly HALF that, so
every spread came out ~half-size — and since NFL/NBA Elo gaps between teams are
modest, that collapsed nearly every game to a ~1-point spread. These are now set
to the standard ~25-Elo-per-point (sport-adjusted): a solid favorite reads as
-4 to -7, a big mismatch double digits, instead of everything at -1.
"""

from __future__ import annotations

import math

ELO_BASE = 1500.0

SPORT_CFG = {
    # ~28 Elo/pt for NBA (higher-scoring, ratings spread wider)
    "nba": {"home_edge": 70.0, "winpct_spread": 600.0, "div": 400.0,
            "avg_total": 226.0, "margin_per_elo": 0.036},
    # ~25 Elo/pt for NFL (the 538 standard) — was 0.020 (double-compressed)
    "nfl": {"home_edge": 50.0, "winpct_spread": 500.0, "div": 400.0,
            "avg_total": 44.0, "margin_per_elo": 0.040},
    # College football: big home-field edge and a huge talent spread
    # (blue-bloods vs cupcakes), higher scoring than the NFL.
    "ncaaf": {"home_edge": 65.0, "winpct_spread": 750.0, "div": 400.0,
              "avg_total": 54.0, "margin_per_elo": 0.040},
    # Men's college basketball: strong home court, wide team-quality spread.
    "ncaab": {"home_edge": 75.0, "winpct_spread": 650.0, "div": 400.0,
              "avg_total": 143.0, "margin_per_elo": 0.038},
    # Women's college basketball: similar shape, slightly lower scoring.
    "wncaab": {"home_edge": 72.0, "winpct_spread": 650.0, "div": 400.0,
               "avg_total": 132.0, "margin_per_elo": 0.036},
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

    EARLY-SEASON BLEND: in Weeks 1-3 every team's win% is ~0.5 (tiny sample), so
    if we leaned on win% the ratings would all cluster and spreads would flatten.
    When explicit Elo ratings are supplied (multi-season carryover), we trust
    them and DON'T dilute with the noisy current win% — that keeps real team
    separation intact early in the year.
    """
    cfg = SPORT_CFG.get(sport, SPORT_CFG["nba"])
    rh = home_rating if home_rating is not None else _winpct_to_rating(home_winpct, cfg["winpct_spread"])
    ra = away_rating if away_rating is not None else _winpct_to_rating(away_winpct, cfg["winpct_spread"])
    rh_adj = rh + cfg["home_edge"]
    prob_home = expected(rh_adj, ra, cfg["div"])
    # expected margin (home minus away points) from the rating gap
    margin = (rh_adj - ra) * cfg["margin_per_elo"]

    # confidence reflects how strong the pick is (its win probability), not merely
    # whether we have records. A lopsided favorite is high-confidence; a near
    # coin-flip is low even with full records. No record at all -> low.
    games_known = (home_winpct is not None and away_winpct is not None)
    have_elo = (home_rating is not None and away_rating is not None)
    if not games_known and not have_elo:
        conf = "low"
    else:
        p = max(prob_home, 1.0 - prob_home)
        conf = "high" if p >= 0.68 else ("medium" if p >= 0.58 else "low")
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": round(margin, 1),       # positive => home favored by this many
        "home_rating": round(rh_adj),
        "home_rating_base": round(rh),        # neutral (pre home edge) for fair power-rating display
        "away_rating": round(ra),
        "home_edge_pts": round(cfg["home_edge"] * cfg["margin_per_elo"], 1),
        "confidence": conf,
        "avg_total": cfg["avg_total"],
    }
