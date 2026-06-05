"""
nhl_model.py — NHL game prediction (Poisson goals model).

Hockey scoring is well-modeled as two Poisson processes, so we estimate each
team's expected goals from their offense (goals-for/game) and the opponent's
defense (goals-against/game), scaled to the league average, with a home-ice
edge. We then sum the joint goal distribution to get a win probability, sending
the tie (regulation draw) mass to OT/shootout split by relative strength.

Mirrors the NCAABB design: a primary stats-based model with a light records
fallback when team stats aren't loaded. Stats come from nhl_provider (file-
backed — no live API calls in the request path).
"""
from __future__ import annotations

import math

LEAGUE_GPG = 3.10     # avg goals per team per game (modern NHL ~3.0-3.2)
HOME_ADV = 1.07       # home teams score ~7% more (multiplicative)
_MAX_GOALS = 10       # truncate the Poisson sum; >10 goals is negligible


def _pois(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _expected_goals(off_gf, opp_ga, home):
    """Expected goals for a team: own attack x opponent's defensive leak, scaled
    to league, with home edge. Returns None if inputs missing."""
    if not off_gf or not opp_ga:
        return None
    att = off_gf / LEAGUE_GPG
    deff = opp_ga / LEAGUE_GPG
    xg = LEAGUE_GPG * att * deff
    return xg * (HOME_ADV if home else 1.0)


def _winprob_from_xg(xg_home, xg_away):
    """P(home win) including OT: sum joint Poisson, split the tie mass by xg."""
    p_home = p_away = p_tie = 0.0
    for i in range(_MAX_GOALS + 1):
        ph = _pois(i, xg_home)
        for j in range(_MAX_GOALS + 1):
            p = ph * _pois(j, xg_away)
            if i > j:
                p_home += p
            elif j > i:
                p_away += p
            else:
                p_tie += p
    ot_home_share = xg_home / (xg_home + xg_away) if (xg_home + xg_away) else 0.5
    return p_home + p_tie * ot_home_share


def _read_stats(home, away):
    try:
        import nhl_provider
        if not nhl_provider.enabled():
            return {}, {}
        return (nhl_provider.get_team_stats_cached(home.get("name", "")),
                nhl_provider.get_team_stats_cached(away.get("name", "")))
    except Exception:
        return {}, {}


def predict_hockey(home, away):
    """home/away are side dicts with at least {'name'}. Returns prob_home,
    exp_margin, confidence, avg_total, factors, model."""
    hs, as_ = _read_stats(home, away)

    xg_home = _expected_goals(hs.get("gf"), as_.get("ga"), home=True)
    xg_away = _expected_goals(as_.get("gf"), hs.get("ga"), home=False)

    if xg_home is not None and xg_away is not None:
        prob_home = _winprob_from_xg(xg_home, xg_away)
        edge = abs(prob_home - 0.5)
        # hockey is high-variance, so edges are smaller than other sports
        conf = "high" if edge > 0.10 else ("medium" if edge > 0.04 else "low")
        factors = [
            f"xG model: {home.get('name')} {xg_home:.2f} vs {away.get('name')} "
            f"{xg_away:.2f} (GF/GA {hs.get('gf')}/{hs.get('ga')} vs "
            f"{as_.get('gf')}/{as_.get('ga')})",
        ]
        if home.get("record") and away.get("record"):
            factors.append(f"Records: {home['record']} vs {away['record']}")
        return {
            "prob_home": round(prob_home, 4),
            "exp_margin": round(xg_home - xg_away, 2),
            "confidence": conf,
            "avg_total": round(xg_home + xg_away, 1),
            "factors": factors,
            "model": "poisson-xg",
        }

    return _fallback_hockey(home, away)


def _fallback_hockey(home, away):
    """Records-only fallback when team stats aren't loaded. Low confidence by
    design — this is the 'data unavailable' path."""
    factors = []
    wh = home.get("win_pct")
    wa = away.get("win_pct")
    if wh is not None and wa is not None:
        # crude: convert points% gap to a probability, plus a small home edge
        prob_home = 0.5 + (wh - wa) * 0.6 + 0.04
        prob_home = max(0.05, min(0.95, prob_home))
        factors.append(f"Records: {home.get('record','?')} vs {away.get('record','?')}")
        conf = "low"
    else:
        prob_home = 0.54  # bare home-ice prior
        conf = "low"
    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": None,
        "confidence": conf,
        "avg_total": None,
        "factors": factors or ["No team stats or records available — home-ice prior only"],
        "model": "fallback",
    }
