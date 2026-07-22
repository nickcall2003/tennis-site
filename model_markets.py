"""
model_markets.py — model-vs-market view for Moneyline / Spread / Total.

One entry point:

    market_view(sport, prob_home, market, exp_home=None, exp_away=None,
                best_of=3, home="Home", away="Away")

`market` is the unified odds record already produced by odds_api / sgo_api:
    {ml_home, ml_away, spread_home, spread_away, spread_home_price,
     spread_away_price, total, total_over_price, total_under_price}
Any subset may be present; anything missing degrades gracefully.

What comes back (all keys always present; values None when unknowable):

  {
    "ml":     {"market": {home, away, home_est, away_est},
               "model":  {home, away, prob_home}},
    "spread": {"market": {line_home, line_away, price_home, price_away, est},
               "model":  {line_home, prob_home_cover, fair_home, fair_away,
                          exp_margin, at_market_line}},
    "total":  {"market": {line, over, under, est},
               "model":  {proj, line, prob_over, fair_over, fair_under,
                          at_market_line}},
    "notes":  [ ...strings the UI can show... ]
  }

Rules encoded here (per the product spec):
  * BOTH sides always get a price. If the book only priced one side, the other
    side is filled by inverting the priced side's implied probability and
    flagged est=True (the UI shows "~").
  * Model prices are FAIR (no vig) from the model's own probabilities.
  * When a market line exists, the model's cover/over probability is computed
    AT that line (apples to apples). The model's own line is also shown.
  * Sports with real score projections (MLB/NCAA-BB via expected runs, soccer
    via expected goals) use the Poisson machinery in betting.py. Sports with
    only a win probability (NBA/WNBA/NFL/NHL/college hoops/football) derive the
    expected margin from the win prob through a Normal margin model with a
    sport-typical sigma — standard practice, and labeled "derived" in notes.
  * Tennis: spread = games handicap, total = total games, both derived from the
    match win prob via betting.tennis machinery.
  * UFC: moneyline model always; market total (rounds) shown when a book posts
    it; there is no meaningful model spread/total for MMA, so those model
    fields stay None with a note.
"""

from __future__ import annotations

import math

import betting

# margin standard deviation by sport (points / goals). Sources: long-run
# closing-line vs result residuals commonly cited for each league.
MARGIN_SIGMA = {
    "nba": 12.0, "wnba": 11.0, "ncaab": 11.5, "wncaab": 11.5,
    "nfl": 13.5, "ncaaf": 15.5,
    "nhl": 2.35,
    "mlb": 3.0, "ncaabb": 3.4,        # fallback only; Poisson used when exp runs known
    "soccer": 1.75,                   # fallback only; Poisson used when exp goals known
}
# total-score standard deviation by sport, for P(over) at a market line when a
# score projection exists but we want a quick Normal read (Poisson used where
# possible instead).
TOTAL_SIGMA = {
    "nba": 18.0, "wnba": 16.0, "ncaab": 17.0, "wncaab": 16.0,
    "nfl": 13.5, "ncaaf": 16.0, "nhl": 2.6,
}

_POISSON_SPORTS = {"mlb", "ncaabb", "soccer"}
_TENNIS = {"tennis"}
_UFC = {"ufc", "mma"}


# ---- odds/probability plumbing ------------------------------------------

def _imp(o):
    if o is None:
        return None
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    return 100.0 / (o + 100.0) if o > 0 else (-o) / ((-o) + 100.0)


def _amer(p):
    if p is None or p <= 0 or p >= 1:
        return None
    return -round(100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def _norm_cdf(x, mu, sigma):
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def _inv_norm(p):
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if p <= 0 or p >= 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _half(x):
    """Round to the nearest half point, nudged off whole numbers (no pushes)."""
    if x is None:
        return None
    v = round(x * 2) / 2
    if v == int(v):
        v += 0.5 if x >= v else -0.5
    return v


def two_sided_ml(ml_home, ml_away):
    """Guarantee both moneyline sides have a price. A missing side is filled by
    inverting the priced side's implied probability (single-book no-vig read)
    and flagged as estimated."""
    home_est = away_est = False
    if ml_home is None and ml_away is not None:
        ip = _imp(ml_away)
        if ip is not None:
            ml_home, home_est = _amer(1 - ip), True
    elif ml_away is None and ml_home is not None:
        ip = _imp(ml_home)
        if ip is not None:
            ml_away, away_est = _amer(1 - ip), True
    return ml_home, ml_away, home_est, away_est


# ---- model margin / totals ----------------------------------------------

def margin_from_prob(prob_home, sport):
    """Expected home margin implied by the win prob under the Normal margin
    model: prob_home = P(margin > 0) = 1 - CDF(0; mu, sigma)  =>
    mu = sigma * InvNorm(prob_home)."""
    sigma = MARGIN_SIGMA.get(sport, 12.0)
    p = min(0.995, max(0.005, prob_home))
    return sigma * _inv_norm(p)


def _poisson_view(exp_home, exp_away, market_spread_home, market_total):
    """Poisson-grid spread/total for run/goal sports. Cover probabilities are
    computed at the MARKET line when one exists (for the honest comparison)."""
    max_n = 22 if exp_home + exp_away > 6 else 14
    ph, pa = betting._poisson_grid(exp_home, exp_away, max_runs=max_n)
    n = len(ph)
    line = market_spread_home if market_spread_home is not None else _half(-(exp_home - exp_away))
    tline = market_total if market_total is not None else _half(exp_home + exp_away)
    p_cover = p_over = 0.0
    for h in range(n):
        for a in range(n):
            j = ph[h] * pa[a]
            if j < 1e-12:
                continue
            if h + line > a:               # home covers home line
                p_cover += j
            if h + a > tline:
                p_over += j
    return {
        "exp_margin": round(exp_home - exp_away, 2),
        "model_line_home": _half(-(exp_home - exp_away)),
        "prob_home_cover": round(p_cover, 3),
        "proj_total": round(exp_home + exp_away, 2),
        "model_total_line": _half(exp_home + exp_away),
        "prob_over": round(p_over, 3),
        "spread_at_line": line, "total_at_line": tline,
    }


def _tennis_games_view(prob_a, best_of, market_spread_a, market_total):
    """Games-handicap + total-games model for tennis, from the match win prob.
    prob_a is the 'home' (player A) win probability."""
    props = betting.tennis_props(prob_a, best_of=best_of)
    tg = props["total_games"]
    exp_games = tg["proj"]
    fav_is_a = prob_a >= 0.5
    g = betting._hold_rates_from_winprob(prob_a, best_of)   # favorite's game rate
    exp_margin_fav = exp_games * (2 * g - 1)                # favorite games margin
    exp_margin_a = exp_margin_fav if fav_is_a else -exp_margin_fav
    sd = 4.2 if best_of == 3 else 5.6                        # games-margin spread
    line_a = market_spread_a if market_spread_a is not None else _half(-exp_margin_a)
    # P(A covers): A margin + line_a > 0
    p_a_cover = 1 - _norm_cdf(-line_a, exp_margin_a, sd)
    tline = market_total if market_total is not None else tg["line"]
    tsd = 3.0 if best_of == 3 else 4.5
    p_over = 1 - _norm_cdf(tline, exp_games, tsd)
    return {
        "exp_margin": round(exp_margin_a, 1),
        "model_line_home": _half(-exp_margin_a),
        "prob_home_cover": round(p_a_cover, 3),
        "proj_total": exp_games,
        "model_total_line": tg["line"],
        "prob_over": round(p_over, 3),
        "spread_at_line": line_a, "total_at_line": tline,
    }


def _normal_view(prob_home, sport, market_spread_home, market_total):
    """Margin-model spread for win-prob-only sports. No model total (a win
    probability says nothing about combined scoring)."""
    sigma = MARGIN_SIGMA.get(sport, 12.0)
    mu = margin_from_prob(prob_home, sport)
    line = market_spread_home if market_spread_home is not None else _half(-mu)
    p_cover = 1 - _norm_cdf(-line, mu, sigma)
    return {
        "exp_margin": round(mu, 1),
        "model_line_home": _half(-mu),
        "prob_home_cover": round(p_cover, 3),
        "proj_total": None,
        "model_total_line": None,
        "prob_over": None,
        "spread_at_line": line, "total_at_line": market_total,
    }


# ---- assembly ------------------------------------------------------------

def market_view(sport, prob_home, market=None, exp_home=None, exp_away=None,
                best_of=3, home="Home", away="Away"):
    sport = (sport or "").lower()
    market = market or {}
    notes = []

    # ----- moneyline -----
    mlh, mla, h_est, a_est = two_sided_ml(market.get("ml_home"), market.get("ml_away"))
    if h_est or a_est:
        notes.append("One moneyline side is estimated (~) by inverting the "
                     "posted side's implied probability.")
    p = None
    try:
        p = float(prob_home)
    except (TypeError, ValueError):
        pass
    ml = {
        "market": {"home": mlh, "away": mla, "home_est": h_est, "away_est": a_est},
        "model": {"home": _amer(p) if p is not None else None,
                  "away": _amer(1 - p) if p is not None else None,
                  "prob_home": round(p, 3) if p is not None else None},
    }

    # ----- model spread/total core -----
    msh = market.get("spread_home")
    msa = market.get("spread_away")
    if msh is None and msa is not None:
        msh = -msa
    mt = market.get("total")

    core = None
    if p is not None:
        if sport in _UFC:
            core = None
            notes.append("MMA has no spread; the total is scheduled rounds and "
                         "the model does not project it.")
        elif sport in _TENNIS:
            core = _tennis_games_view(p, best_of or 3, msh, mt)
            notes.append("Tennis spread is the games handicap and the total is "
                         "total games, both derived from the match win "
                         "probability.")
        elif sport in _POISSON_SPORTS and exp_home is not None and exp_away is not None:
            core = _poisson_view(float(exp_home), float(exp_away), msh, mt)
        else:
            core = _normal_view(p, sport, msh, mt)
            if sport not in _POISSON_SPORTS:
                notes.append("Model spread is derived from the win probability "
                             "through a margin model (sigma "
                             f"{MARGIN_SIGMA.get(sport, 12.0):g}).")
            if core["prob_over"] is None and mt is not None:
                notes.append("No model total for this sport — the win "
                             "probability carries no information about "
                             "combined scoring, so only the market total is "
                             "shown.")

    # ----- spread -----
    sp_ph = market.get("spread_home_price")
    sp_pa = market.get("spread_away_price")
    sp_est = False
    if sp_ph is None and sp_pa is not None:
        ip = _imp(sp_pa)
        sp_ph, sp_est = _amer(1 - ip) if ip else None, ip is not None
    elif sp_pa is None and sp_ph is not None:
        ip = _imp(sp_ph)
        sp_pa, sp_est = _amer(1 - ip) if ip else None, ip is not None
    spread = {
        "market": {"line_home": msh,
                   "line_away": (msa if msa is not None
                                 else (-msh if msh is not None else None)),
                   "price_home": sp_ph, "price_away": sp_pa, "est": sp_est},
        "model": None,
    }
    if core is not None:
        pc = core["prob_home_cover"]
        spread["model"] = {
            "line_home": core["model_line_home"],
            "exp_margin": core["exp_margin"],
            "prob_home_cover": pc,
            "fair_home": _amer(pc), "fair_away": _amer(1 - pc) if pc is not None else None,
            "at_market_line": msh is not None,
        }

    # ----- total -----
    t_over = market.get("total_over_price")
    t_under = market.get("total_under_price")
    t_est = False
    if t_over is None and t_under is not None:
        ip = _imp(t_under)
        t_over, t_est = _amer(1 - ip) if ip else None, ip is not None
    elif t_under is None and t_over is not None:
        ip = _imp(t_over)
        t_under, t_est = _amer(1 - ip) if ip else None, ip is not None
    total = {
        "market": {"line": mt, "over": t_over, "under": t_under, "est": t_est},
        "model": None,
    }
    if core is not None and core["prob_over"] is not None:
        po = core["prob_over"]
        total["model"] = {
            "proj": core["proj_total"],
            "line": core["model_total_line"],
            "prob_over": po,
            "fair_over": _amer(po), "fair_under": _amer(1 - po),
            "at_market_line": mt is not None,
        }

    return {"ml": ml, "spread": spread, "total": total, "notes": notes,
            "home": home, "away": away, "sport": sport}
