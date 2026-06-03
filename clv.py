"""
Betting-performance math: odds conversions, CLV, ROI, and units.

All functions are pure and unit-tested. Definitions:

- A "unit" is one standard bet. We assume FLAT staking of 1 unit per pick
  (the honest default; no fractional Kelly unless the user opts in later).
- ROI = (total profit in units) / (total units staked) * 100.
- CLV (Closing Line Value) compares the odds you took to the closing odds.
  Positive CLV means you beat the close -- the single best long-run skill
  signal. We express CLV as the percentage-point edge in implied probability:
      CLV%% = implied_prob(close) - implied_prob(taken)
  (taken at a better price than close => you locked value => positive CLV).
"""


def american_to_decimal(odds):
    if odds is None:
        return None
    odds = float(odds)
    if odds > 0:
        return 1 + odds / 100.0
    return 1 + 100.0 / abs(odds)


def american_to_prob(odds):
    """Implied win probability from American odds (with vig)."""
    if odds is None:
        return None
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def profit_units(odds, won, stake=1.0):
    """Profit in units for a single settled bet at the given American odds."""
    dec = american_to_decimal(odds)
    if dec is None:
        return 0.0
    return stake * (dec - 1.0) if won else -stake


def clv_pct(taken_odds, close_odds):
    """
    Closing line value as implied-probability edge in percentage points.
    Positive => you took a better price than the close.
    """
    pt = american_to_prob(taken_odds)
    pc = american_to_prob(close_odds)
    if pt is None or pc is None:
        return None
    # you took odds implying pt; market closed implying pc. If pc > pt, the
    # price shortened after you bet -> you had value -> positive CLV.
    return round((pc - pt) * 100, 2)


def summarize(bets):
    """
    bets: list of dicts each with keys:
        odds (american, taken), won (bool), close_odds (optional),
        stake (optional, default 1)
    Returns aggregate units, ROI, record, and average CLV.
    """
    staked = profit = 0.0
    wins = losses = 0
    clvs = []
    for b in bets:
        stake = b.get("stake", 1.0)
        staked += stake
        p = profit_units(b.get("odds"), b.get("won"), stake)
        profit += p
        if b.get("won"):
            wins += 1
        else:
            losses += 1
        if b.get("close_odds") is not None and b.get("odds") is not None:
            c = clv_pct(b["odds"], b["close_odds"])
            if c is not None:
                clvs.append(c)
    roi = (profit / staked * 100) if staked else None
    avg_clv = (sum(clvs) / len(clvs)) if clvs else None
    beat_close = sum(1 for c in clvs if c > 0)
    return {
        "bets": wins + losses, "wins": wins, "losses": losses,
        "units_staked": round(staked, 2), "units_won": round(profit, 2),
        "roi": round(roi, 2) if roi is not None else None,
        "avg_clv": round(avg_clv, 2) if avg_clv is not None else None,
        "clv_sample": len(clvs),
        "beat_close": beat_close,
        "beat_close_pct": round(100 * beat_close / len(clvs)) if clvs else None,
    }
