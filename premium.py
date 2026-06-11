"""
premium.py — the paid "why it's a best bet" layer.

On top of the standard analysis every game gets, a Best Bet earns three extra
facts, all computed from data we already have on hand:

  1. slate standing  — how this play ranks against the rest of today's board
                       (overall and within its own sport).
  2. stake sizing    — a model-derived unit suggestion: quarter-Kelly off the
                       live market line when one is attached, otherwise a plain
                       confidence ladder.
  3. track record    — the model's settled win/loss on this sport over a
                       rolling window, honest about thin samples.

Everything degrades gracefully. No market line -> ladder staking. No history
-> an honest "no track record yet" note. premium_facts() returns BOTH the
structured facts (so Claude can narrate them in stage 3) and a plain-text
block (the template fallback used whenever the API is off or errors).

Tunable via env:
  STAKE_MAX_UNITS       cap on the unit suggestion           (default 3.0)
  STAKE_KELLY_FRACTION  fraction of full Kelly to recommend  (default 0.25)
  TRACKREC_DAYS         rolling window for track record      (default 90)
  TRACKREC_MIN_N        settled picks before we trust a %    (default 10)
"""

import os
import datetime as dt


# --------------------------------------------------------------------------
# odds helpers
# --------------------------------------------------------------------------
def _american_to_decimal(a):
    if a is None:
        return None
    try:
        a = int(a)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return 1.0 + (a / 100.0 if a > 0 else 100.0 / abs(a))


def _fmt_odds(a):
    if a is None:
        return "n/a"
    a = int(a)
    return f"+{a}" if a > 0 else str(a)


# --------------------------------------------------------------------------
# 1. slate standing — rank against the rest of today's board
# --------------------------------------------------------------------------
def _pid(p):
    return (p.get("sport"), str(p.get("id")))


def slate_standing(play, all_plays):
    """Where this play ranks today, overall and within its own sport."""
    key = _pid(play)
    ordered = sorted(all_plays or [],
                     key=lambda p: -p.get("score_key", p.get("prob", 0)))
    overall_total = len(ordered)
    overall_rank = next((i + 1 for i, p in enumerate(ordered) if _pid(p) == key),
                        None)

    same = [p for p in ordered if p.get("sport") == play.get("sport")]
    sport_total = len(same)
    sport_rank = next((i + 1 for i, p in enumerate(same) if _pid(p) == key), None)

    out = {"overall_rank": overall_rank, "overall_total": overall_total,
           "sport_rank": sport_rank, "sport_total": sport_total}
    if overall_rank and overall_total:
        # "top X% of today's board" — smaller is better, floor at 1%
        out["top_pct"] = max(1, round(100 * overall_rank / overall_total))
    return out


# --------------------------------------------------------------------------
# 2. stake sizing — quarter-Kelly off the live line, else a confidence ladder
# --------------------------------------------------------------------------
def stake_suggestion(play):
    max_u = float(os.environ.get("STAKE_MAX_UNITS", "3"))
    kf = float(os.environ.get("STAKE_KELLY_FRACTION", "0.25"))
    p = play.get("prob")
    price = play.get("market_odds")        # the real book line, if odds source on

    if p is not None and price is not None:
        dec = _american_to_decimal(price)
        if dec and dec > 1:
            b = dec - 1.0
            f_full = (b * p - (1.0 - p)) / b      # full-Kelly fraction of bankroll
            if f_full <= 0:
                return {"units": 0.0, "priced": True, "pass": True,
                        "kelly": round(f_full, 4), "price": price,
                        "basis": f"no value at {_fmt_odds(price)}"}
            # 1 unit == 1% of bankroll, so units = fraction * 100
            units = f_full * kf * 100.0
            units = max(0.5, min(max_u, round(units * 2) / 2.0))
            return {"units": units, "priced": True, "pass": False,
                    "kelly": round(f_full, 4), "price": price,
                    "basis": f"quarter-Kelly at {_fmt_odds(price)}"}

    # --- no live line: confidence/probability ladder ---
    if p is None:
        return {"units": 1.0, "priced": False, "pass": False,
                "basis": "flat, no probability"}
    if p >= 0.68:
        u = 2.0
    elif p >= 0.62:
        u = 1.5
    elif p >= 0.57:
        u = 1.0
    else:
        u = 0.5
    return {"units": u, "priced": False, "pass": False,
            "basis": "model confidence ladder, no live line yet"}


# --------------------------------------------------------------------------
# 3. track record — the model's settled W-L on this sport over a window
# --------------------------------------------------------------------------
def _summarize_record(rows, min_n):
    n = len(rows)
    wins = sum(1 for r in rows if getattr(r, "correct", False))
    out = {"n": n, "wins": wins, "losses": n - wins,
           "min_n": min_n, "enough": n >= min_n}
    if n:
        out["pct"] = round(100 * wins / n)
    return out


def track_record(sport, session_factory):
    days = int(os.environ.get("TRACKREC_DAYS", "90"))
    min_n = int(os.environ.get("TRACKREC_MIN_N", "10"))
    base = {"n": 0, "wins": 0, "losses": 0, "min_n": min_n,
            "enough": False, "days": days}
    if not sport or session_factory is None:
        return base
    try:
        from models import PickResult
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
        with session_factory() as db:
            rows = (db.query(PickResult)
                    .filter(PickResult.sport == sport,
                            PickResult.settled_date >= cutoff)
                    .all())
        rec = _summarize_record(rows, min_n)
        rec["days"] = days
        return rec
    except Exception as e:
        base["error"] = str(e)
        return base


# --------------------------------------------------------------------------
# template fallback prose (also the fact sheet Claude narrates in stage 3)
# --------------------------------------------------------------------------
def _render(play, facts):
    sport = (play.get("sport") or "").upper()
    st, sk, rc = facts["standing"], facts["stake"], facts["record"]
    out = []

    # standing
    orank, ototal = st.get("overall_rank"), st.get("overall_total")
    if orank and ototal:
        top = st.get("top_pct")
        if st.get("sport_rank") == 1 and st.get("sport_total", 0) > 1:
            seg = (f"It's the model's top {sport} play today and #{orank} "
                   f"across all {ototal} games on the board")
        else:
            srank, stot = st.get("sport_rank"), st.get("sport_total")
            sp = f"#{srank} of {stot} {sport} games today and " if srank and stot else ""
            seg = f"It ranks {sp}#{orank} of {ototal} across the full board"
        if top is not None and ototal >= 5:
            seg += f" — top {top}%"
        out.append(seg + ".")

    # stake
    if sk.get("pass"):
        out.append(f"At {_fmt_odds(sk.get('price'))} the model sees no value, "
                   f"so this is a pass — no recommended stake.")
    else:
        u = sk.get("units")
        ustr = (f"{u:g} unit" + ("s" if u != 1 else ""))
        out.append(f"Suggested stake: {ustr} ({sk.get('basis')}).")

    # track record
    if rc.get("enough"):
        out.append(f"Over its last {rc['n']} settled {sport} picks "
                   f"({rc.get('days', 90)}d), the model is "
                   f"{rc['wins']}-{rc['losses']} ({rc.get('pct')}%).")
    elif rc.get("n"):
        out.append(f"Track record is still thin here — {rc['wins']}-{rc['losses']} "
                   f"over {rc['n']} settled {sport} picks so far.")
    else:
        out.append(f"No settled {sport} history yet to show a track record.")

    return " ".join(out)


# --------------------------------------------------------------------------
# public: assemble the premium layer for one play
# --------------------------------------------------------------------------
def premium_facts(play, all_plays, session_factory):
    """Structured premium facts + a plain-text block, for one Best Bet."""
    facts = {
        "standing": slate_standing(play, all_plays),
        "stake": stake_suggestion(play),
        "record": track_record(play.get("sport"), session_factory),
    }
    facts["text"] = _render(play, facts)
    return facts
