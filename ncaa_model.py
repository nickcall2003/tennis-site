"""
ncaa_model.py — prediction models for NCAA sports.

College Baseball (predict_baseball):
  Inputs come from ESPN (records, AP rank) plus optional Warren Nolan RPI.
  We compute an Elo-style rating for each team from:
    - season win% (ESPN record)            -> base strength
    - RPI rank (Warren Nolan, if present)  -> strength-of-schedule correction
    - AP/curated rank (ESPN, if present)    -> small prestige nudge
  Then convert the rating gap + home-field edge into a win probability.

  HONEST LIMITATION: this is a STRENGTH model, not a full run-expectancy model.
  ESPN's free college feed doesn't reliably expose team ERA/OBP/SLG or the
  weekend rotation, so we don't fabricate those. When richer stats are available
  later (or via a paid feed), the MLB run-expectancy engine can be layered in.
"""
from __future__ import annotations

import math

HOME_EDGE_ELO = 45.0      # college home-field is meaningful in baseball
_BASE = 1500.0


def _winpct_to_elo(wp):
    """Map a season win% (0..1) to an Elo-ish rating around 1500."""
    if wp is None:
        return _BASE
    # spread: a .800 team ~ +210, a .300 team ~ -210
    return _BASE + (wp - 0.5) * 700.0


def _rpi_rank_to_adj(rank):
    """
    RPI rank -> Elo adjustment. RPI encodes schedule strength, which win% alone
    misses (a .700 team in a weak league is worse than a .600 team in the SEC).
    #1 ~ +160, #50 ~ +40, #150 ~ -30, #300 ~ -120 (smooth log curve).
    """
    if not rank or rank < 1:
        return 0.0
    # log-decay: strong teams get a bonus, weak ranks a penalty, centered ~#80
    return max(-140.0, min(170.0, 150.0 - 92.0 * math.log10(rank)))


def _expected(elo_a, elo_b):
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def _team_elo(side, rpi):
    elo = _winpct_to_elo(side.get("win_pct"))
    if rpi and rpi.get("rpi_rank"):
        # blend: record-Elo is the base, RPI nudges for schedule strength
        elo += _rpi_rank_to_adj(rpi["rpi_rank"]) * 0.6
    if side.get("rank"):           # AP/curated top-25 prestige nudge
        elo += max(0.0, (26 - side["rank"])) * 3.0
    return elo


def predict_baseball(home, away):
    """home/away are the ESPN _side dicts. Returns prob_home, exp_margin, etc."""
    factors = []
    rpi_home = rpi_away = {}
    try:
        import warrennolan
        if warrennolan.available():
            rpi_home = warrennolan.get_rating(home.get("name", ""))
            rpi_away = warrennolan.get_rating(away.get("name", ""))
    except Exception:
        pass

    eh = _team_elo(home, rpi_home) + HOME_EDGE_ELO
    ea = _team_elo(away, rpi_away)
    prob_home = _expected(eh, ea)

    # expected run margin: map Elo gap to runs (college games swing more than MLB)
    gap = eh - ea
    exp_margin = round(gap / 95.0, 1)   # ~ +1 run per 95 Elo

    # confidence: how much real signal we have
    have_rpi = bool(rpi_home or rpi_away)
    have_rec = home.get("win_pct") is not None and away.get("win_pct") is not None
    edge = abs(prob_home - 0.5)
    if have_rpi and have_rec and edge > 0.12:
        confidence = "high"
    elif have_rec and edge > 0.06:
        confidence = "medium"
    else:
        confidence = "low"

    # human-readable factors for the analysis card
    if have_rec:
        factors.append(f"Records: {home.get('record','?')} vs {away.get('record','?')}")
    if rpi_home.get("rpi_rank") or rpi_away.get("rpi_rank"):
        hr = rpi_home.get("rpi_rank", "NR")
        ar = rpi_away.get("rpi_rank", "NR")
        factors.append(f"RPI: #{hr} vs #{ar} (Warren Nolan)")
    if home.get("rank") or away.get("rank"):
        factors.append(f"AP rank: {home.get('rank') or 'NR'} vs {away.get('rank') or 'NR'}")

    return {
        "prob_home": round(prob_home, 4),
        "exp_margin": exp_margin,
        "confidence": confidence,
        "avg_total": None,        # no reliable scoring model on free college data
        "factors": factors,
        "rpi_home": rpi_home.get("rpi_rank"),
        "rpi_away": rpi_away.get("rpi_rank"),
    }
