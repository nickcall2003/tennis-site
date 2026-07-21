"""
tennis_gate.py — wager gate for tennis. NOT a model change.

WHY THIS EXISTS
---------------
The tennis audit over 1,044 tracked wagers found the model is well calibrated at
every probability band (50-55% -> 51.0% actual, 80%+ -> 85.7% actual). The losses
come entirely from WHICH PRICES GET WAGERED:

  * 829 picks were wagered at a NEGATIVE model edge      -> -43.42u
  * 1,047 picks were heavy favorites (<= -200) winning
    74.5%, which loses by construction at those prices   -> -76.36u
  * the 5-20% claimed-edge band is where the model
    disagrees with the market and is wrong               -> -37.09u
    (0-5% edge, by contrast, was +5.24u)

So: leave the probabilities alone, and stop taking the prices that lose.

WHAT IT DOES
------------
`check()` returns (allowed, reason). A blocked pick is still PREDICTED and still
DISPLAYED with its price and edge — only the tracked wager is suppressed. The
reason string is attached to the pick and counted, because the ladder bug hid for
days behind a silent drop.

EVERYTHING IS ENV-TUNABLE (no deploy needed to change a threshold):

  TENNIS_GATE_ENABLED   "1"/"0"   default "1"    master switch
  TENNIS_MIN_EDGE       float     default 0.0    skip if edge_pct <= this
  TENNIS_MAX_EDGE       float     default 5.0    skip if edge_pct > this
                                                 ("" or "off" disables the cap)
  TENNIS_MAX_FAV_ODDS   int       default -250   skip if the price is shorter
                                                 than this (e.g. -300 is shorter)
  TENNIS_REQUIRE_EDGE   "1"/"0"   default "1"    skip when edge can't be computed

Edges are in PERCENTAGE POINTS (5.0 == 5%), matching `edge_pct` on the pick.

TUNING NOTES
------------
TENNIS_MAX_EDGE=5 is deliberately aggressive. It also discards the 20%+ bucket,
which showed +7.64u — but on only 143 picks, which is noise. If a larger sample
later shows that bucket is real, set TENNIS_MAX_EDGE=off and add a band exclusion
instead. Start tight; loosen with evidence.
"""

from __future__ import annotations

import os
from collections import Counter, deque

# --- reasons ---------------------------------------------------------------
NEGATIVE_EDGE = "negative_edge"
EDGE_TOO_HIGH = "edge_above_max"
FAV_TOO_SHORT = "fav_price_too_short"
NO_EDGE = "no_edge_computed"

REASON_TEXT = {
    NEGATIVE_EDGE: "model edge is not positive at this price",
    EDGE_TOO_HIGH: "claimed edge is in the band the audit showed is noise",
    FAV_TOO_SHORT: "favorite price is shorter than the break-even the model supports",
    NO_EDGE: "no de-vigged market edge could be computed",
}

# Bounded record of recent decisions, for /api/tennis/gate-diag.
_counts = Counter()
_recent = deque(maxlen=60)


def _flag(name, default):
    return str(os.environ.get(name, default)).strip().lower() not in ("0", "false", "no", "off", "")


def _num(name, default):
    """Float from env. Returns None when explicitly disabled ('', 'off', 'none')."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("", "off", "none"):
        return None
    try:
        return float(raw)
    except ValueError:
        return default


def config():
    """Current live settings — read from env on every call so a Railway variable
    change takes effect on restart without touching code."""
    return {
        "enabled": _flag("TENNIS_GATE_ENABLED", "1"),
        "min_edge": _num("TENNIS_MIN_EDGE", 0.0),
        "max_edge": _num("TENNIS_MAX_EDGE", 5.0),
        "max_fav_odds": _num("TENNIS_MAX_FAV_ODDS", -250.0),
        "require_edge": _flag("TENNIS_REQUIRE_EDGE", "1"),
    }


def check(edge_pct=None, market_odds=None, ref=None, pick=None):
    """Decide whether this tennis pick may become a TRACKED WAGER.

    Returns (allowed: bool, reason: str | None). `reason` is None when allowed.
    Never raises — a gate that throws would take the tennis board down with it.
    """
    try:
        cfg = config()
        if not cfg["enabled"]:
            return True, None

        reason = None

        # 1. No computable edge -> we have no honest basis for the wager.
        if edge_pct is None:
            if cfg["require_edge"]:
                reason = NO_EDGE

        else:
            e = float(edge_pct)

            # 2. Non-positive edge. The single largest loss category (-43.42u),
            #    and a pure logic bug: the model said -EV and we bet it anyway.
            if cfg["min_edge"] is not None and e <= cfg["min_edge"]:
                reason = NEGATIVE_EDGE

            # 3. Claimed edge above the band that actually profits.
            elif cfg["max_edge"] is not None and e > cfg["max_edge"]:
                reason = EDGE_TOO_HIGH

        # 4. Price cap on favorites, independent of edge. At -300 you need 75%
        #    to break even and -500 needs 83%; the model hit 74.5% at these
        #    prices, so the category loses by construction.
        if reason is None and cfg["max_fav_odds"] is not None and market_odds is not None:
            try:
                mo = float(market_odds)
                if mo < 0 and mo < cfg["max_fav_odds"]:
                    reason = FAV_TOO_SHORT
            except (TypeError, ValueError):
                pass

        _counts[reason or "allowed"] += 1
        if reason:
            _recent.append({"ref": str(ref) if ref is not None else None,
                            "pick": pick, "edge_pct": edge_pct,
                            "market_odds": market_odds, "reason": reason})
        return (reason is None), reason
    except Exception as e:                     # never block the board on a gate bug
        print(f"[tennis_gate] check failed, allowing: {type(e).__name__}: {e}")
        return True, None


def stats():
    """Counts and a sample of recent skips, for the diagnostic endpoint. Counters
    are per-process and reset on restart — they show what the gate is doing right
    now, not a historical ledger."""
    return {
        "config": config(),
        "counts": dict(_counts),
        "recent_skips": list(_recent)[-25:],
        "reason_text": REASON_TEXT,
    }
