"""
betting.py
----------
Turns the model's own outputs into betting-style probabilities. This is YOUR
edge, computed from the same engine that powers the predictions — not scraped
"sharp money" data (which is proprietary and would have to be invented).

For MLB we have expected runs per team (Poisson), which cleanly gives:
  - moneyline win prob (already have it)
  - run line (-1.5 / +1.5)  -> P(favorite wins by 2+)
  - total over/under         -> P(total runs > line)
  - a fair-odds American price for each

For tennis we have a single match win prob, from which we derive:
  - set score distribution (2-0 / 2-1, or best-of-5 variants)
  - total games over/under (approx from per-set hold rates)

Everything here is a probability the user can compare to a sportsbook's number
themselves. Not betting advice.
"""

from __future__ import annotations

import math


# ---- shared helpers -----------------------------------------------------

def american_odds(prob: float) -> str:
    """Fair American odds string for a probability (no vig)."""
    if prob <= 0 or prob >= 1:
        return "\u2014"
    if prob >= 0.5:
        return f"-{round(100 * prob / (1 - prob))}"
    return f"+{round(100 * (1 - prob) / prob)}"


def _poisson_pmf(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _poisson_grid(er_home, er_away, max_runs=22):
    ph = [_poisson_pmf(k, er_home) for k in range(max_runs + 1)]
    pa = [_poisson_pmf(k, er_away) for k in range(max_runs + 1)]
    return ph, pa


# ---- MLB ----------------------------------------------------------------

def mlb_lines(er_home, er_away, total_line=None):
    """
    Returns spread (run line), total, and moneyline probabilities from the two
    expected-run means. total_line defaults to the nearest half-run to the
    model's own projected total.
    """
    ph, pa = _poisson_grid(er_home, er_away)
    n = len(ph)
    p_home_ml = p_home_cover = p_away_cover = 0.0
    # joint distribution
    total_dist = {}
    for h in range(n):
        for a in range(n):
            joint = ph[h] * pa[a]
            if joint < 1e-12:
                continue
            if h > a:
                p_home_ml += joint
            if h - a >= 2:        # home -1.5 covers
                p_home_cover += joint
            if a - h >= 2:        # away -1.5 covers
                p_away_cover += joint
            t = h + a
            total_dist[t] = total_dist.get(t, 0.0) + joint
    # ties split for moneyline (extra innings ~ coin flip)
    p_tie = sum(ph[k] * pa[k] for k in range(n))
    p_home_ml += 0.5 * p_tie

    proj_total = er_home + er_away
    if total_line is None:
        total_line = round(proj_total * 2) / 2          # nearest 0.5
        if total_line == int(total_line):               # avoid push lines
            total_line += 0.5
    p_over = sum(p for t, p in total_dist.items() if t > total_line)

    fav_home = er_home >= er_away
    return {
        "moneyline": {
            "home": round(p_home_ml, 3), "away": round(1 - p_home_ml, 3),
            "home_odds": american_odds(p_home_ml), "away_odds": american_odds(1 - p_home_ml),
        },
        "runline": {
            "line": 1.5, "favorite": "home" if fav_home else "away",
            "fav_cover": round(p_home_cover if fav_home else p_away_cover, 3),
            "dog_cover": round(1 - (p_home_cover if fav_home else p_away_cover), 3),
        },
        "total": {
            "line": total_line, "proj": round(proj_total, 2),
            "over": round(p_over, 3), "under": round(1 - p_over, 3),
        },
    }


# ---- Tennis -------------------------------------------------------------

def _hold_rates_from_winprob(p_match, best_of=3):
    """
    Back out a plausible per-point edge from the match win prob, then derive a
    per-game hold rate. This is an approximation: it maps match prob -> a single
    'game win' probability for the favorite via a smooth curve calibrated so
    that a 50% match is ~50% games and an 80% match is ~58% games.
    """
    # favorite match prob >= 0.5
    fav = max(p_match, 1 - p_match)
    # gentle mapping: each 1% of match edge ~ 0.4% of game edge
    game_edge = 0.5 + (fav - 0.5) * 0.40
    return min(0.72, max(0.50, game_edge))


def tennis_set_scores(p_match, best_of=3):
    """
    Probability of each set score for the FAVORITE, plus total-games lean.
    Best-of-3: 2-0, 2-1 (and the underdog mirrors). Best-of-5: 3-0/3-1/3-2.
    Derived from a per-set win prob implied by the match prob.
    """
    fav_match = max(p_match, 1 - p_match)
    # implied per-set win prob for the favorite (sets are the unit that matters)
    # invert: P(win best-of-3) = s^2 + 2 s^2 (1-s) ... solve approximately via search
    def bo3(s):
        return s * s + 2 * s * s * (1 - s)
    def bo5(s):
        return (s**3) + 3 * (s**3) * (1 - s) + 6 * (s**3) * (1 - s) ** 2
    target = fav_match
    lo, hi = 0.5, 0.99
    f = bo5 if best_of == 5 else bo3
    for _ in range(40):
        mid = (lo + hi) / 2
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2

    scores = []
    if best_of == 5:
        p30 = s**3
        p31 = 3 * (s**3) * (1 - s)
        p32 = 6 * (s**3) * (1 - s) ** 2
        scores = [("3-0", p30), ("3-1", p31), ("3-2", p32)]
    else:
        p20 = s * s
        p21 = 2 * s * s * (1 - s)
        scores = [("2-0", p20), ("2-1", p21)]
    tot = sum(p for _, p in scores) or 1
    scores = [(k, round(v / tot * fav_match, 3)) for k, v in scores]  # normalize to fav win prob
    return {"per_set_win": round(s, 3), "fav_scores": scores}


def tennis_lines(p_match, best_of=3):
    info = tennis_set_scores(p_match, best_of)
    # straight-sets probability (no dropped set) = first score in list
    straight = info["fav_scores"][0][1]
    return {
        "set_scores": info["fav_scores"],
        "straight_sets": round(straight, 3),
        "per_set_win": info["per_set_win"],
    }


def tennis_props(p_match, best_of=3):
    """
    Derive tennis 'props': total games over/under and the set-score market.
    More lopsided matches -> fewer total games (more straight-set blowouts).
    """
    info = tennis_set_scores(p_match, best_of)
    fav = max(p_match, 1 - p_match)
    # expected games: closer matches go longer. Rough model per set ~9.5 games,
    # scaled by competitiveness, plus an extra set when not straight.
    straight = info["fav_scores"][0][1] / fav if fav else 0.5   # P(straight | fav wins) approx
    sets_played = (2 if best_of == 3 else 3) + (1 - straight) * (1 if best_of == 3 else 1.4)
    competitiveness = 1 - abs(fav - 0.5) * 0.6      # even matches => longer games
    games_per_set = 8.5 + 2.0 * competitiveness
    exp_games = round(sets_played * games_per_set, 1)
    line = round(exp_games * 2) / 2
    if line == int(line):
        line += 0.5
    # variance on total games ~ sd 3.0 (bo3) / 4.5 (bo5)
    sd = 3.0 if best_of == 3 else 4.5
    p_over = 1 - _normal_cdf(line, exp_games, sd)
    return {
        "total_games": {"line": line, "proj": exp_games,
                        "over": round(p_over, 3), "under": round(1 - p_over, 3)},
        "set_scores": info["fav_scores"],
    }


# ---- NBA / NFL ----------------------------------------------------------

def _normal_cdf(x, mu, sigma):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def team_lines(prob_home, exp_margin, sport="nba", spread=None):
    """
    Spread/total/moneyline probabilities for a team sport from the model's
    win prob + expected margin. Margins are modeled Normal around exp_margin
    with a sport-typical standard deviation.
        NBA: sigma ~ 12 points;  NFL: sigma ~ 13.5 points
    `spread` is the home line (negative = home favored). Defaults to the
    model's own expected margin rounded to a half-point.
    """
    sigma = 12.0 if sport == "nba" else 13.5
    if spread is None:
        spread = -round(exp_margin * 2) / 2          # home line ~ -exp_margin
    # P(home covers spread): home margin + spread > 0  => margin > -spread
    p_home_cover = 1 - _normal_cdf(-spread, exp_margin, sigma)
    fav = "home" if exp_margin >= 0 else "away"
    return {
        "moneyline": {
            "home": round(prob_home, 3), "away": round(1 - prob_home, 3),
            "home_odds": american_odds(prob_home), "away_odds": american_odds(1 - prob_home),
        },
        "spread": {
            "home_line": spread, "favorite": fav,
            "home_cover": round(p_home_cover, 3), "away_cover": round(1 - p_home_cover, 3),
            "exp_margin": round(exp_margin, 1),
        },
    }
