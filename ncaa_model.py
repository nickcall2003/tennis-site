"""
ncaa_model.py — prediction models for NCAA sports.

College Baseball (predict_baseball):
  Inputs come from ESPN (records, AP rank) plus optional Warren Nolan RPI and,
  when available, team stats (runs/game, ERA) for a run-expectancy estimate.

  Two prediction paths, picked automatically by data availability:
    1. RUN-EXPECTANCY (preferred): team R/G + ERA -> run model, THEN blended
       with Warren Nolan RPI so schedule strength (who you played) corrects what
       raw team stats miss. This is the sharpest estimate.
    2. STRENGTH (fallback): records + RPI Elo. Always fast.

  HONEST LIMITATION: the run model uses team-level R/G and ERA, not the specific
  weekend starter. Per-game probable-pitcher data would be the next real upgrade
  (and the run model already has separate starter/bullpen slots ready for it).
"""
from __future__ import annotations

import math

HOME_EDGE_ELO = 45.0      # college home-field is meaningful in baseball
_BASE = 1500.0

# How much the RPI (schedule-strength) estimate pulls the run-model probability.
# 0.0 = ignore RPI in the run path (old behavior); 1.0 = RPI only. 0.30 is a
# gentle, orthogonal correction. Tune here if you want RPI to swing harder.
RPI_BLEND = 0.30


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


def _rpi_prob_home(rpi_home, rpi_away):
    """Win prob for HOME from RPI ranks alone (incl. home edge), or None if we
    don't have both ranks. Orthogonal to R/G + ERA: it's about who you played."""
    if not (rpi_home and rpi_home.get("rpi_rank") and rpi_away and rpi_away.get("rpi_rank")):
        return None
    eh = _BASE + _rpi_rank_to_adj(rpi_home["rpi_rank"]) + HOME_EDGE_ELO
    ea = _BASE + _rpi_rank_to_adj(rpi_away["rpi_rank"])
    return _expected(eh, ea)


def _team_elo(side, rpi):
    elo = _winpct_to_elo(side.get("win_pct"))
    if rpi and rpi.get("rpi_rank"):
        # blend: record-Elo is the base, RPI nudges for schedule strength
        elo += _rpi_rank_to_adj(rpi["rpi_rank"]) * 0.6
    if side.get("rank"):           # AP/curated top-25 prestige nudge
        elo += max(0.0, (26 - side["rank"])) * 3.0
    return elo


def _read_rpi(home, away):
    """Cache-only RPI lookup for both sides (never triggers a fetch/parse)."""
    rpi_home = rpi_away = {}
    try:
        import warrennolan
        if warrennolan.cached_ready():
            rpi_home = warrennolan.get_rating_cached(home.get("name", ""))
            rpi_away = warrennolan.get_rating_cached(away.get("name", ""))
    except Exception:
        pass
    return rpi_home, rpi_away


def predict_baseball(home, away, allow_fetch=False):
    """home/away are the ESPN _side dicts. Returns prob_home, exp_margin, etc.

    Tries the run-expectancy path first (R/G + ERA, blended with RPI); falls
    back to the strength model (records + RPI) when team stats aren't available.
    """
    hl = _runexp_baseball(home, away, allow_fetch=allow_fetch)
    if hl is not None:
        return hl
    return _strength_baseball(home, away)


def _runexp_baseball(home, away, allow_fetch=False):
    """Use team stats (runs/game, ERA) + the MLB run-expectancy engine, then
    blend in Warren Nolan RPI for schedule strength. Returns None if team stats
    aren't cached (so we fall back to the strength model).
    By default only uses already-cached stats (no network) so it can't hang."""
    try:
        import highlightly
        if not highlightly.enabled():
            return None
        if allow_fetch:
            hs = highlightly.get_team_stats(home.get("name", ""))
            as_ = highlightly.get_team_stats(away.get("name", ""))
        else:
            hs = highlightly.get_team_stats_cached(home.get("name", ""))
            as_ = highlightly.get_team_stats_cached(away.get("name", ""))
        # need at least offense + some pitching signal on both sides
        if not (hs.get("rpg") and as_.get("rpg")):
            return None
        from mlb_model import TeamInput, GameFactors, predict_game
        # college run environment differs from MLB; set a college league baseline
        gf = GameFactors(lg_runs=5.4, lg_era=5.4)   # D1 scoring is higher than MLB
        h = TeamInput(name=home.get("name"), runs_per_game=hs.get("rpg"),
                      starter_era=hs.get("era"), bullpen_era=hs.get("era"))
        a = TeamInput(name=away.get("name"), runs_per_game=as_.get("rpg"),
                      starter_era=as_.get("era"), bullpen_era=as_.get("era"))
        r = predict_game(h, a, gf)

        prob_rm = r["prob_home"]

        # --- Blend in RPI (schedule strength) — orthogonal to R/G + ERA -------
        rpi_home, rpi_away = _read_rpi(home, away)
        prob_rpi = _rpi_prob_home(rpi_home, rpi_away)
        if prob_rpi is not None and RPI_BLEND > 0:
            prob_home = RPI_BLEND * prob_rpi + (1.0 - RPI_BLEND) * prob_rm
        else:
            prob_home = prob_rm

        factors = [f"Run model: {home.get('name')} {hs.get('rpg',0):.1f} R/G "
                   f"(ERA {hs.get('era','?')}) vs {away.get('name')} "
                   f"{as_.get('rpg',0):.1f} R/G (ERA {as_.get('era','?')}) — NCAA stats"]
        if rpi_home.get("rpi_rank") or rpi_away.get("rpi_rank"):
            hr = rpi_home.get("rpi_rank", "NR")
            ar = rpi_away.get("rpi_rank", "NR")
            factors.append(f"RPI: #{hr} vs #{ar} (Warren Nolan)")
        if home.get("record") and away.get("record"):
            factors.append(f"Records: {home['record']} vs {away['record']}")

        edge = abs(prob_home - 0.5)
        conf = "high" if edge > 0.12 else ("medium" if edge > 0.05 else "low")
        return {
            "prob_home": round(prob_home, 4),
            "exp_margin": round(r["exp_runs_home"] - r["exp_runs_away"], 1),
            "confidence": conf,
            "avg_total": round(r["exp_runs_home"] + r["exp_runs_away"], 1),
            "factors": factors,
            "rpi_home": rpi_home.get("rpi_rank"),
            "rpi_away": rpi_away.get("rpi_rank"),
            "model": "run-expectancy+rpi" if prob_rpi is not None else "run-expectancy",
        }
    except Exception as e:
        print(f"[ncaa_model] run-exp path failed: {e}")
        return None


def _strength_baseball(home, away):
    """home/away are the ESPN _side dicts. Returns prob_home, exp_margin, etc."""
    factors = []
    rpi_home, rpi_away = _read_rpi(home, away)

    eh = _team_elo(home, rpi_home) + HOME_EDGE_ELO
    ea = _team_elo(away, rpi_away)
    prob_home = _expected(eh, ea)

    gap = eh - ea
    exp_margin = round(gap / 95.0, 1)

    have_rpi = bool(rpi_home or rpi_away)
    have_rec = home.get("win_pct") is not None and away.get("win_pct") is not None
    edge = abs(prob_home - 0.5)
    if have_rpi and have_rec and edge > 0.12:
        confidence = "high"
    elif have_rec and edge > 0.06:
        confidence = "medium"
    else:
        confidence = "low"

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
        "avg_total": None,
        "factors": factors,
        "rpi_home": rpi_home.get("rpi_rank"),
        "rpi_away": rpi_away.get("rpi_rank"),
        "model": "strength",
    }
