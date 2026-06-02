"""
mlb_model.py
------------
A principled single-game MLB win-probability model.

The idea (standard, defensible sabermetrics):
  1. Estimate each team's EXPECTED RUNS in this specific game from:
       - the team's offense (runs/game vs league average)
       - the OPPONENT's run prevention TODAY = blend of the announced
         starting pitcher (≈60%, ~6 IP) and the bullpen (≈40%, ~3 IP)
       - the ballpark's run-scoring factor
       - weather (hot air + wind blowing out -> more runs)
       - a modest home-field edge
  2. Model each team's runs as independent Poisson(expected runs) and
     compute P(home wins) = P(home runs > away runs) (+ half the tie mass,
     since extra innings are ~a coin flip).

Everything is expressed as multipliers around league average, so the model
degrades gracefully: any missing input falls back to "league average" (1.0)
rather than breaking. Lower ERA = better pitching = fewer runs allowed.

No model is ever "right" — strong public MLB models land around 55–58% on the
moneyline. This gives an honest, factor-driven number, not a guarantee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# League baselines (roughly 2024–25; the provider can override at runtime).
LG_RUNS_PER_GAME = 4.4
LG_ERA = 4.10

# How a start is split between the starter and the bullpen (innings share).
STARTER_WEIGHT = 0.60
BULLPEN_WEIGHT = 0.40

# Home teams win ~54% all else equal; modeled as a small run nudge.
HOME_RUN_BUMP = 1.035      # home offense plays up ~3.5%
AWAY_RUN_DAMP = 0.965


@dataclass
class TeamInput:
    name: str
    abbr: str = ""
    team_id: int | None = None
    runs_per_game: float | None = None      # team offense (scored)
    starter_name: str | None = None
    starter_era: float | None = None        # announced starter, season ERA
    starter_ip: float | None = None         # innings (confidence signal)
    bullpen_era: float | None = None        # relievers' aggregate ERA
    logo: str | None = None


@dataclass
class GameFactors:
    park_factor: float = 1.0                 # 1.00 neutral; Coors ~1.12, Petco ~0.95
    weather_factor: float = 1.0              # >1 boosts runs (hot, wind out)
    lg_runs: float = LG_RUNS_PER_GAME
    lg_era: float = LG_ERA
    notes: list = field(default_factory=list)


def _mult(value, league, lo=0.6, hi=1.6):
    """Ratio vs league average, clamped so one extreme stat can't dominate."""
    if not value or value <= 0 or not league:
        return 1.0
    return max(lo, min(hi, value / league))


def pitching_multiplier(starter_era, bullpen_era, lg_era):
    """
    Runs the opponent is expected to score against this staff, vs league.
    >1 means worse-than-average pitching (gives up more runs).
    """
    s = _mult(starter_era, lg_era)
    b = _mult(bullpen_era, lg_era)
    if starter_era and bullpen_era:
        return STARTER_WEIGHT * s + BULLPEN_WEIGHT * b
    return s if starter_era else (b if bullpen_era else 1.0)


def expected_runs(off_rpg, opp_starter_era, opp_bullpen_era, gf: GameFactors):
    off = _mult(off_rpg, gf.lg_runs)
    prevent = pitching_multiplier(opp_starter_era, opp_bullpen_era, gf.lg_era)
    return gf.lg_runs * off * prevent * gf.park_factor * gf.weather_factor


def _poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def win_probability(er_home, er_away, max_runs=20):
    """P(home wins) under independent Poisson run distributions, ties split."""
    ph = [_poisson_pmf(k, er_home) for k in range(max_runs + 1)]
    pa = [_poisson_pmf(k, er_away) for k in range(max_runs + 1)]
    p_home = p_tie = 0.0
    for h in range(max_runs + 1):
        for a in range(max_runs + 1):
            joint = ph[h] * pa[a]
            if h > a:
                p_home += joint
            elif h == a:
                p_tie += joint
    return p_home + 0.5 * p_tie


def confidence(home: TeamInput, away: TeamInput):
    have = lambda t: (t.runs_per_game and (t.starter_era or t.bullpen_era))
    thin = lambda t: (t.starter_ip is not None and t.starter_ip < 20)
    if not (have(home) and have(away)):
        return "low"
    if thin(home) or thin(away) or not (home.starter_era and away.starter_era):
        return "medium"
    return "high"


def predict_game(home: TeamInput, away: TeamInput, gf: GameFactors | None = None):
    """Returns dict: prob_home, expected runs, confidence, and a factor breakdown."""
    gf = gf or GameFactors()
    er_home = expected_runs(home.runs_per_game, away.starter_era, away.bullpen_era, gf) * HOME_RUN_BUMP
    er_away = expected_runs(away.runs_per_game, home.starter_era, home.bullpen_era, gf) * AWAY_RUN_DAMP
    prob_home = win_probability(er_home, er_away)
    return {
        "prob_home": round(prob_home, 4),
        "exp_runs_home": round(er_home, 2),
        "exp_runs_away": round(er_away, 2),
        "confidence": confidence(home, away),
        "factors": {
            "home_offense_x": round(_mult(home.runs_per_game, gf.lg_runs), 3),
            "away_offense_x": round(_mult(away.runs_per_game, gf.lg_runs), 3),
            "home_staff_x": round(pitching_multiplier(home.starter_era, home.bullpen_era, gf.lg_era), 3),
            "away_staff_x": round(pitching_multiplier(away.starter_era, away.bullpen_era, gf.lg_era), 3),
            "park_factor": gf.park_factor,
            "weather_factor": gf.weather_factor,
        },
    }
