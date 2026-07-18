"""
ladder.py — the daily "Ladder Challenge": $10 rolled through 10 straight winning
picks, each the model's single best edge priced +100 to -120.

RULES (as specified):
  * ONE pick per day — the highest model-vs-market edge inside the odds band.
  * Win  -> bankroll rolls forward, rung += 1. Hit rung 10 -> run complete, reset.
  * Loss -> reset to rung 1 / $10, new attempt (the busted run is kept in history).
  * No qualifying pick today -> SKIP the day. Never force a bad pick onto the ladder.
  * Its OWN record: never touches the site's pick record, units, ROI, or props.

Everything is best-effort and must never break a page.
"""
import datetime as dt

from sqlalchemy import select

from models import LadderState, LadderLeg

import os

BAND_LO = -120       # most negative american odds allowed
BAND_HI = 120        # most positive
RUNGS = 10
START = 10.0
# a ladder leg must clear a real edge, not a rounding-error edge. Your example
# (market -108, model -200) is a huge edge and qualifies easily; a +0.2% edge does
# not. Tunable via LADDER_MIN_EDGE.
MIN_EDGE = float(os.environ.get("LADDER_MIN_EDGE", "3.0"))


def _dec(american):
    a = float(american)
    return 1 + (a / 100 if a > 0 else 100 / abs(a))


def _in_band(o):
    try:
        return BAND_LO <= int(o) <= BAND_HI
    except (TypeError, ValueError):
        return False


def _state(db):
    s = db.execute(select(LadderState)).scalars().first()
    if not s:
        s = LadderState(rung=1, bankroll=START, start_bankroll=START, attempt=1,
                        best_bankroll_ever=START)
        db.add(s)
        db.commit()
    return s


def best_leg(picks):
    """The single best in-band edge among today's picks, or None.
    Filters on MARKET odds in the +100/-120 band AND a real model edge over it."""
    best = None
    for p in picks or []:
        o, e = p.get("market_odds"), p.get("edge_pct")
        if o is None or e is None or not _in_band(o):
            continue                 # market price must sit in the ladder band
        try:
            if float(e) < MIN_EDGE:  # and the model must have a genuine edge on it
                continue
        except (TypeError, ValueError):
            continue
        if best is None or float(e) > float(best.get("edge_pct") or 0):
            best = p
    return best


def _implied(american):
    a = float(american)
    return (100 / (a + 100)) if a > 0 else (abs(a) / (abs(a) + 100))


def best_combo(picks, max_legs=3):
    """When no single pick sits in the band, combine 2-3 value FAVORITES whose
    COMBINED odds land in the +/-band. Each leg must be a genuine value play: the
    model's win prob must beat the market's implied prob by MIN_EDGE — so a -400
    the model rates even higher qualifies, but a -400 it secretly sees as -180 does
    NOT. Legs are from different games (independent, no correlation)."""
    favs = []
    for p in picks or []:
        o = p.get("market_odds")
        prob = p.get("prob")
        if o is None or prob is None:
            continue
        try:
            o = int(o)
            prob = float(prob)
        except (TypeError, ValueError):
            continue
        if o >= 0:                     # combos are built from favorites
            continue
        edge = (prob - _implied(o)) * 100      # model prob vs market implied
        if edge < MIN_EDGE:            # must be real value, not a fake favorite
            continue
        gid = p.get("id") or p.get("game_id") or p.get("match")
        favs.append({"p": p, "odds": o, "prob": prob, "edge": edge, "gid": gid,
                     "dec": _dec(o)})
    favs.sort(key=lambda x: -x["edge"])          # best value first
    # greedily grow a combo until its combined american odds enters the band
    import itertools
    best = None
    for n in range(2, max_legs + 1):
        for combo in itertools.combinations(favs[:8], n):
            if len({c["gid"] for c in combo}) != n:
                continue                          # different games only
            dec = 1.0
            for c in combo:
                dec *= c["dec"]
            amer = round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))
            if not _in_band(amer):
                continue
            combo_prob = 1.0
            for c in combo:
                combo_prob *= c["prob"]
            combo_edge = sum(c["edge"] for c in combo) / n     # avg leg edge
            cand = {"amer": amer, "dec": dec, "prob": combo_prob,
                    "avg_edge": round(combo_edge, 1), "legs": combo}
            if best is None or combo_prob > best["prob"]:
                best = cand
    return best


def todays_pick(db, day=None, picks=None):
    """Return today's ladder leg (existing if already chosen, else pick one). Skips
    the day silently if nothing qualifies."""
    day = day or dt.date.today()
    lo = dt.datetime.combine(day, dt.time.min)
    hi = dt.datetime.combine(day, dt.time.max)
    existing = db.execute(
        select(LadderLeg).where(LadderLeg.pick_date >= lo, LadderLeg.pick_date <= hi)
    ).scalars().first()
    if existing:
        return existing
    if picks is None:
        return None
    leg = best_leg(picks)
    from promo_routes import _pick_line
    if leg:
        try:
            pick_txt, _ = _pick_line(leg)
        except Exception:
            pick_txt = leg.get("pick") or "?"
        odds = int(leg.get("market_odds"))
        edge = leg.get("edge_pct")
        gref = str(leg.get("id") or leg.get("game_id") or leg.get("match") or "")
    else:
        # no single in-band edge -> try a value-favorite combo that reaches the band
        combo = best_combo(picks)
        if not combo:
            return None                 # skip the day
        names = []
        for c in combo["legs"]:
            try:
                t, _ = _pick_line(c["p"])
            except Exception:
                t = c["p"].get("pick") or "?"
            o = c["odds"]
            names.append(f"{t} ({o})")
        pick_txt = " + ".join(names)     # e.g. "Yankees (-400) + Dodgers (-380)"
        odds = combo["amer"]
        edge = combo["avg_edge"]
        gref = "combo:" + ",".join(str(c["gid"]) for c in combo["legs"])
    s = _state(db)
    row = LadderLeg(
        pick_date=dt.datetime.combine(day, dt.time(12, 0)),
        attempt=s.attempt, rung=s.rung,
        sport=(leg.get("sport") if leg else "combo") or "",
        game_ref=gref,
        pick=pick_txt[:160], odds=odds, edge_pct=edge,
        stake=round(s.bankroll, 2),
        to_return=round(s.bankroll * _dec(odds), 2), result=None, settled=False)
    db.add(row)
    db.commit()
    return row


def settle_leg(db, leg, won):
    """Grade the day's leg and roll or reset the challenge."""
    if leg.settled:
        return
    s = _state(db)
    leg.result = "win" if won else "loss"
    leg.settled = True
    if won:
        s.bankroll = leg.to_return
        s.best_bankroll_ever = max(s.best_bankroll_ever, s.bankroll)
        s.best_rung_ever = max(s.best_rung_ever, leg.rung)
        if leg.rung >= RUNGS:
            s.completed_runs += 1            # ran the whole ladder!
            s.rung, s.bankroll, s.attempt = 1, START, s.attempt + 1
        else:
            s.rung = leg.rung + 1
    else:
        s.best_rung_ever = max(s.best_rung_ever, leg.rung)
        s.rung, s.bankroll, s.attempt = 1, START, s.attempt + 1   # reset
    s.updated = dt.datetime.utcnow()
    db.commit()


def ladder_record(db):
    """Full performance record of the ladder challenge — its OWN W-L, units, ROI.
    Every settled daily leg counts once at a flat 1u risk, so this is an honest
    'how do the ladder picks do' scoreboard, separate from the site's main record."""
    legs = db.execute(
        select(LadderLeg).where(LadderLeg.settled == True)  # noqa: E712
    ).scalars().all()
    w = sum(1 for l in legs if l.result == "win")
    losses = sum(1 for l in legs if l.result == "loss")
    graded = w + losses
    # flat-stake units: +decimal_profit on a win, -1 on a loss (1u risked per leg)
    units = 0.0
    for l in legs:
        if l.result == "win" and l.odds is not None:
            units += (_dec(l.odds) - 1)
        elif l.result == "loss":
            units -= 1
    return {
        "graded": graded, "wins": w, "losses": losses,
        "win_pct": round(100 * w / graded, 1) if graded else None,
        "units": round(units, 2),
        "roi_pct": round(100 * units / graded, 1) if graded else None,
        "note": "Ladder's own record — 1u flat per daily leg. Separate from the "
                "site win/loss record, units, and ROI.",
    }


def state_summary(db):
    s = _state(db)
    legs = db.execute(
        select(LadderLeg).order_by(LadderLeg.pick_date.desc()).limit(15)
    ).scalars().all()
    return {
        "attempt": s.attempt, "current_rung": s.rung, "bankroll": round(s.bankroll, 2),
        "target": round(START * (1.9 ** RUNGS)),   # rough headline target
        "best_rung_ever": s.best_rung_ever,
        "best_bankroll_ever": round(s.best_bankroll_ever, 2),
        "completed_runs": s.completed_runs,
        "record": ladder_record(db),
        "history": [{
            "date": l.pick_date.date().isoformat(), "attempt": l.attempt, "rung": l.rung,
            "pick": l.pick, "odds": l.odds, "stake": l.stake, "to_return": l.to_return,
            "result": l.result,
        } for l in legs],
        "note": "Its own record — separate from the site's pick record, units, and ROI.",
    }


def reset_challenge(db, wipe_history=True):
    """Start the challenge over: attempt 1, rung 1, $10 bankroll, and (by default)
    clear the leg history so the record/units/ROI go back to 0-0. Used when a run
    was never really live (e.g. the picker was broken and no leg was ever posted)."""
    deleted = 0
    if wipe_history:
        legs = db.execute(select(LadderLeg)).scalars().all()
        deleted = len(legs)
        for l in legs:
            db.delete(l)
    s = _state(db)
    s.rung = 1
    s.bankroll = START
    s.start_bankroll = START
    s.attempt = 1
    s.best_rung_ever = 0
    s.best_bankroll_ever = START
    s.completed_runs = 0
    s.updated = dt.datetime.utcnow()
    db.commit()
    return {"ok": True, "legs_deleted": deleted, "attempt": 1, "rung": 1,
            "bankroll": START}
