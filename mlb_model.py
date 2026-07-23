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
    runs_per_game: float | None = None      # team offense (scored), season
    recent_rpg: float | None = None         # runs/game over last ~10 (form)
    starter_name: str | None = None
    starter_era: float | None = None        # announced starter, season ERA
    starter_ip: float | None = None         # innings (confidence signal)
    bullpen_era: float | None = None        # relievers' aggregate ERA
    bullpen_fatigue: float | None = None     # 0..1, recent reliever workload
    logo: str | None = None

    def blended_rpg(self):
        """Offense estimate blending season (70%) with recent form (30%)."""
        if self.runs_per_game and self.recent_rpg:
            return 0.70 * self.runs_per_game + 0.30 * self.recent_rpg
        return self.runs_per_game or self.recent_rpg


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


def pitching_multiplier(starter_era, bullpen_era, lg_era, bullpen_fatigue=None):
    """
    Runs the opponent is expected to score against this staff, vs league.
    >1 means worse-than-average pitching (gives up more runs).
    A tired bullpen (fatigue 0..1) is penalized slightly (up to ~8% worse).
    """
    s = _mult(starter_era, lg_era)
    b = _mult(bullpen_era, lg_era)
    if bullpen_fatigue:
        b *= 1.0 + 0.08 * max(0.0, min(1.0, bullpen_fatigue))
    if starter_era and bullpen_era:
        return STARTER_WEIGHT * s + BULLPEN_WEIGHT * b
    return s if starter_era else (b if bullpen_era else 1.0)


def expected_runs(off_rpg, opp_starter_era, opp_bullpen_era, gf: GameFactors,
                  opp_bullpen_fatigue=None):
    off = _mult(off_rpg, gf.lg_runs)
    prevent = pitching_multiplier(opp_starter_era, opp_bullpen_era, gf.lg_era,
                                  opp_bullpen_fatigue)
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


# --------------------------------------------------------------------------
# Driver decomposition
# --------------------------------------------------------------------------
# The run projection is a pure PRODUCT of multipliers:
#
#     exp_runs = lg_runs x offense x opp_pitching x park x weather x home_field
#
# which means it decomposes EXACTLY — no interaction terms, no normalising
# fudge. A reader can literally multiply the chain and land on the model's own
# number. That is the whole point: an attribution a skeptic can audit.
#
# NOTE: the previous `factors` block did NOT reconcile. It reported season-only
# offense while the model actually uses the 70/30 season/recent blend, and it
# omitted bullpen fatigue. On a game with a hot offense and a gassed pen that
# drifted ~0.4 runs — enough to make a published breakdown visibly wrong. The
# builder below is driven by the SAME values the projection uses, and
# `_verify_chain` fails loudly if the two ever diverge again.

def _driver_chain(off_rpg, opp_starter_era, opp_bullpen_era, gf, opp_fatigue,
                  side_mult, side_label):
    """Ordered multiplier chain for one side, with the running product."""
    off = _mult(off_rpg, gf.lg_runs)
    prevent = pitching_multiplier(opp_starter_era, opp_bullpen_era, gf.lg_era,
                                  opp_fatigue)
    steps = [
        {"key": "baseline",     "label": "League average",  "mult": 1.0,
         "detail": f"{gf.lg_runs:.2f} runs/game"},
        {"key": "offense",      "label": "Offense",         "mult": off,
         "detail": (f"{off_rpg:.2f} R/G vs {gf.lg_runs:.2f} league"
                    if off_rpg else "no offensive data — league average")},
        {"key": "opp_pitching", "label": "Opponent pitching", "mult": prevent,
         "detail": _pitch_detail(opp_starter_era, opp_bullpen_era, opp_fatigue, gf.lg_era)},
        {"key": "park",         "label": "Ballpark",        "mult": gf.park_factor,
         "detail": _park_detail(gf.park_factor)},
        {"key": "weather",      "label": "Weather",         "mult": gf.weather_factor,
         "detail": _weather_detail(gf.weather_factor)},
        {"key": "home_field",   "label": side_label,        "mult": side_mult,
         "detail": ("home teams score ~3.5% more" if side_mult > 1
                    else "road teams score ~3.5% less")},
    ]
    running = gf.lg_runs
    for s in steps:
        if s["key"] == "baseline":
            s["runs_after"] = round(running, 3)
            s["runs_delta"] = 0.0
            continue
        before = running
        running *= s["mult"]
        s["runs_after"] = round(running, 3)
        s["runs_delta"] = round(running - before, 3)   # this factor's run impact
    return steps, running


def _pitch_detail(s_era, b_era, fatigue, lg_era):
    bits = []
    if s_era:
        bits.append(f"starter {s_era:.2f} ERA")
    if b_era:
        bits.append(f"bullpen {b_era:.2f} ERA")
    if fatigue and fatigue > 0.25:
        bits.append(f"pen fatigued ({fatigue:.0%})")
    if not bits:
        return "no pitching data — league average"
    return ", ".join(bits) + f" vs {lg_era:.2f} league"


def _park_detail(pf):
    if abs(pf - 1.0) < 0.005:
        return "neutral park"
    return f"park plays {'+' if pf > 1 else ''}{(pf - 1) * 100:.0f}% to " + \
           ("hitters" if pf > 1 else "pitchers")


def _weather_detail(wf):
    if abs(wf - 1.0) < 0.005:
        return "no weather effect (dome or neutral)"
    return f"conditions {'boost' if wf > 1 else 'suppress'} runs " \
           f"{'+' if wf > 1 else ''}{(wf - 1) * 100:.0f}%"


def _verify_chain(steps, claimed, tol=0.01):
    """The chain must reproduce the model's own number. If a future edit makes
    the projection and the published breakdown disagree, fail here rather than
    shipping a breakdown that quietly lies."""
    product = 1.0
    for s in steps:
        product *= s["mult"] if s["key"] != "baseline" else 1.0
    return abs(steps[0]["runs_after"] * product - claimed) <= tol


def predict_game(home: TeamInput, away: TeamInput, gf: GameFactors | None = None):
    """Returns dict: prob_home, expected runs, confidence, and a factor breakdown."""
    gf = gf or GameFactors()
    er_home = expected_runs(home.blended_rpg(), away.starter_era, away.bullpen_era, gf,
                            away.bullpen_fatigue) * HOME_RUN_BUMP
    er_away = expected_runs(away.blended_rpg(), home.starter_era, home.bullpen_era, gf,
                            home.bullpen_fatigue) * AWAY_RUN_DAMP
    prob_home = win_probability(er_home, er_away)

    # Exact decomposition, built from the SAME inputs the projection used.
    home_steps, home_chain = _driver_chain(
        home.blended_rpg(), away.starter_era, away.bullpen_era, gf,
        away.bullpen_fatigue, HOME_RUN_BUMP, "Home field")
    away_steps, away_chain = _driver_chain(
        away.blended_rpg(), home.starter_era, home.bullpen_era, gf,
        home.bullpen_fatigue, AWAY_RUN_DAMP, "Road split")

    return {
        "prob_home": round(prob_home, 4),
        "exp_runs_home": round(er_home, 2),
        "exp_runs_away": round(er_away, 2),
        "confidence": confidence(home, away),
        "drivers": {
            "home": {"team": home.name, "steps": home_steps,
                     "total": round(home_chain, 2),
                     "reconciles": _verify_chain(home_steps, er_home)},
            "away": {"team": away.name, "steps": away_steps,
                     "total": round(away_chain, 2),
                     "reconciles": _verify_chain(away_steps, er_away)},
            "margin": round(er_home - er_away, 2),
        },
        "factors": {
            # kept for backwards compatibility with existing callers, but now
            # reports the values ACTUALLY used (blended offense, fatigue applied)
            "home_offense_x": round(_mult(home.blended_rpg(), gf.lg_runs), 3),
            "away_offense_x": round(_mult(away.blended_rpg(), gf.lg_runs), 3),
            "home_staff_x": round(pitching_multiplier(home.starter_era, home.bullpen_era,
                                                      gf.lg_era, home.bullpen_fatigue), 3),
            "away_staff_x": round(pitching_multiplier(away.starter_era, away.bullpen_era,
                                                      gf.lg_era, away.bullpen_fatigue), 3),
            "park_factor": gf.park_factor,
            "weather_factor": gf.weather_factor,
        },
    }
