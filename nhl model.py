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


LEAGUE_SV = 0.905     # league-average save percentage
LEAGUE_PP = 0.205     # league-average power-play conversion
LEAGUE_PK = 0.795     # league-average penalty-kill success


def _pct(v):
    """Coerce a pp/pk value to a 0-1 fraction (accepts 22.5 or 0.225)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1:
        v /= 100.0
    return v if 0 < v < 1 else None


def _st_factor(pp, opp_pk):
    """Multiplier on a team's expected goals from the special-teams matchup:
    their power-play conversion vs the opponent's penalty kill. A strong PP
    against a leaky kill lifts xG; the reverse trims it. Clamped to ±7%."""
    pp, opp_pk = _pct(pp), _pct(opp_pk)
    if pp is None or opp_pk is None:
        return 1.0, None
    adv = (pp - LEAGUE_PP) + (LEAGUE_PK - opp_pk)   # PP above avg + opp kill below avg
    return 1.0 + max(-0.07, min(0.07, adv)), (pp, opp_pk)


def _goalie_factor(sv):
    """Multiplier on the goals a goalie's team allows: a goalie above league
    save% suppresses opponent xG, a backup below it inflates it. Clamped so a
    small-sample save% can't swing a game absurdly."""
    if not sv:
        return 1.0
    try:
        sv = float(sv)
    except (TypeError, ValueError):
        return 1.0
    if sv <= 0.0 or sv >= 1.0:
        return 1.0
    return max(0.78, min(1.22, (1.0 - sv) / (1.0 - LEAGUE_SV)))


def predict_hockey(home, away):
    """home/away are side dicts with at least {'name'}. Returns prob_home,
    exp_margin, confidence, avg_total, factors, model. If a starting goalie save%
    is present (home['goalie_sv']/away['goalie_sv']) it scales opponent xG."""
    hs, as_ = _read_stats(home, away)

    xg_home = _expected_goals(hs.get("gf"), as_.get("ga"), home=True)
    xg_away = _expected_goals(as_.get("gf"), hs.get("ga"), home=False)

    if xg_home is not None and xg_away is not None:
        # starting goalie: away's goalie faces home's offense, and vice-versa
        gf_away = _goalie_factor(away.get("goalie_sv"))
        gf_home = _goalie_factor(home.get("goalie_sv"))
        xg_home *= gf_away
        xg_away *= gf_home
        # special teams: each offense's PP vs the other team's PK
        st_home, sth = _st_factor(hs.get("pp_pct"), as_.get("pk_pct"))
        st_away, sta = _st_factor(as_.get("pp_pct"), hs.get("pk_pct"))
        xg_home *= st_home
        xg_away *= st_away
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
        if home.get("goalie_sv") or away.get("goalie_sv"):
            factors.append(
                f"Goalies: {home.get('goalie_name','home')} "
                f"{('%.3f'%float(home['goalie_sv'])) if home.get('goalie_sv') else '—'} vs "
                f"{away.get('goalie_name','away')} "
                f"{('%.3f'%float(away['goalie_sv'])) if away.get('goalie_sv') else '—'}")
        if sth or sta:
            ph = f"{round(sth[0]*100)}%" if sth else "—"
            pa = f"{round(sta[0]*100)}%" if sta else "—"
            kh = f"{round(sta[1]*100)}%" if sta else "—"
            ka = f"{round(sth[1]*100)}%" if sth else "—"
            factors.append(
                f"Special teams: {home.get('name')} PP {ph} vs {away.get('name')} PK {ka}"
                f" · {away.get('name')} PP {pa} vs {home.get('name')} PK {kh}")
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
