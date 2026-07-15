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

BAND_LO = -120       # most negative american odds allowed
BAND_HI = 100        # most positive
RUNGS = 10
START = 10.0


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
    """The single best in-band edge among today's picks, or None."""
    best = None
    for p in picks or []:
        o, e = p.get("market_odds"), p.get("edge_pct")
        if o is None or e is None or not _in_band(o):
            continue
        try:
            if float(e) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        if best is None or float(e) > float(best.get("edge_pct") or 0):
            best = p
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
    if not leg:
        return None                       # skip the day
    s = _state(db)
    from promo_routes import _pick_line
    try:
        pick_txt, _ = _pick_line(leg)
    except Exception:
        pick_txt = leg.get("pick") or "?"
    odds = int(leg.get("market_odds"))
    row = LadderLeg(
        pick_date=dt.datetime.combine(day, dt.time(12, 0)),
        attempt=s.attempt, rung=s.rung, sport=leg.get("sport") or "",
        game_ref=str(leg.get("id") or leg.get("game_id") or leg.get("match") or ""),
        pick=pick_txt[:160], odds=odds, edge_pct=leg.get("edge_pct"),
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
        "history": [{
            "date": l.pick_date.date().isoformat(), "attempt": l.attempt, "rung": l.rung,
            "pick": l.pick, "odds": l.odds, "stake": l.stake, "to_return": l.to_return,
            "result": l.result,
        } for l in legs],
        "note": "Its own record — separate from the site's pick record, units, and ROI.",
    }
