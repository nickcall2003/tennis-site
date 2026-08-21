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


# How many days a leg may stay unsettled before the ladder stops waiting on it.
# Without a cap, a single leg whose result never arrives (e.g. the grader stopped
# running, or the game_ref can't be matched) blocks EVERY future day — the ladder
# keeps re-posting that same stale leg forever. After this many days we grade it
# from whatever result we can find, and if we truly can't find one we void the leg
# (no bankroll change) so the challenge can move on. Tunable via LADDER_STALE_DAYS.
STALE_DAYS = int(os.environ.get("LADDER_STALE_DAYS", "2"))


def _result_from_picks(leg, picks):
    """Try to determine win/loss for a leg from a picks list that carries results.
    Returns True (win), False (loss), or None (unknown / not final yet).

    Matches the leg to its pick by game_ref, then reads whatever result field the
    feed provides. Combo legs (game_ref 'combo:a,b,c') win only if ALL parts win."""
    if not picks:
        return None
    by_ref = {}
    for p in picks or []:
        ref = str(p.get("id") or p.get("game_id") or p.get("match") or "")
        if ref:
            by_ref[ref] = p

    def _leg_won(p):
        # Accept a few common shapes so this works regardless of the feed:
        #   p["result"] in {"win","loss"} / p["correct"] bool / p["won"] bool
        r = p.get("result")
        if isinstance(r, str):
            rl = r.strip().lower()
            if rl in ("win", "won", "w"):
                return True
            if rl in ("loss", "lost", "l"):
                return False
        for k in ("correct", "won", "is_win"):
            if isinstance(p.get(k), bool):
                return p[k]
        return None

    ref = leg.game_ref or ""
    if ref.startswith("combo:"):
        parts = [x for x in ref[len("combo:"):].split(",") if x]
        results = []
        for part in parts:
            p = by_ref.get(part)
            if not p:
                return None                  # missing a leg -> not gradable yet
            r = _leg_won(p)
            if r is None:
                return None                  # any leg unknown -> whole combo unknown
            results.append(r)
        if not results:
            return None
        return all(results)                  # combo wins only if every leg wins

    p = by_ref.get(ref)
    if not p:
        return None
    return _leg_won(p)


def _auto_settle_stale(db, picks=None, now=None):
    """Grade any unsettled legs from PRIOR days so the ladder never gets stuck.
    This makes the ladder self-healing: it no longer depends on an external grader
    running. A leg is graded when its result is known; if a leg has been pending
    longer than STALE_DAYS and its result still can't be found, it is VOIDED
    (deleted, no bankroll change) so a single unresolvable leg can't freeze the
    challenge forever — which is exactly what caused the same pick to re-post daily.

    Returns the number of legs acted on. Best-effort; never raises to the caller."""
    now = now or dt.datetime.utcnow()
    today_lo = dt.datetime.combine((now.date()), dt.time.min)
    acted = 0
    try:
        stale = db.execute(
            select(LadderLeg)
            .where(LadderLeg.settled == False,        # noqa: E712
                   LadderLeg.pick_date < today_lo)
            .order_by(LadderLeg.pick_date.asc())
        ).scalars().all()
    except Exception as e:
        print(f"[ladder] stale scan failed: {e}")
        return 0

    for leg in stale:
        won = _result_from_picks(leg, picks)
        if won is not None:
            try:
                settle_leg(db, leg, bool(won))
                acted += 1
            except Exception as e:
                print(f"[ladder] settle failed for leg {leg.id}: {e}")
            continue
        # Result still unknown. If the leg is old enough, stop waiting on it.
        age_days = (now - leg.pick_date).days
        if age_days >= STALE_DAYS:
            try:
                db.delete(leg)               # void: no bankroll change, unblock ladder
                db.commit()
                acted += 1
                print(f"[ladder] voided stale unsettled leg from "
                      f"{leg.pick_date.date().isoformat()} (no result after "
                      f"{age_days}d) so the challenge can advance")
            except Exception as e:
                print(f"[ladder] void failed for leg {leg.id}: {e}")
    return acted


def todays_pick(db, day=None, picks=None):
    """Return today's ladder leg (existing if already chosen, else pick one). Skips
    the day silently if nothing qualifies."""
    day = day or dt.date.today()
    lo = dt.datetime.combine(day, dt.time.min)
    hi = dt.datetime.combine(day, dt.time.max)

    # FIRST: settle/clear any stale prior legs so we never re-post an old leg that
    # never got graded (the "same pick every day, never refreshes" bug). This makes
    # the ladder self-healing and independent of any external grading job.
    _auto_settle_stale(db, picks=picks)
    existing = db.execute(
        select(LadderLeg).where(LadderLeg.pick_date >= lo, LadderLeg.pick_date <= hi)
    ).scalars().first()
    if existing:
        # A pending leg must always reflect the CURRENT state. Today's leg is
        # chosen at post time, but yesterday's leg often settles afterwards — so
        # a leg created at rung 1 with a $10 stake needs to roll forward once the
        # win lands and the bankroll becomes $19.09. Only unsettled legs move.
        if not existing.settled and existing.result is None:
            s = _state(db)
            changed = False
            if existing.rung != s.rung or existing.attempt != s.attempt:
                existing.rung, existing.attempt = s.rung, s.attempt
                changed = True
            stake = round(s.bankroll, 2)
            if existing.stake != stake:
                existing.stake = stake
                changed = True
            if existing.odds is not None:
                exp = round(stake * _dec(existing.odds), 2)
                if existing.to_return != exp:
                    existing.to_return = exp
                    changed = True
            if changed:
                db.commit()
        return existing

    # Never open a new rung while an EARLIER leg is still ungraded. Without this
    # a second leg appears while the first is pending, the pending one stops
    # being "current", and the rung freezes — a won rung can sit unsettled
    # forever while the bankroll never advances. Return the outstanding leg so
    # the post shows what is actually still live.
    prior = db.execute(
        select(LadderLeg)
        .where(LadderLeg.settled == False,          # noqa: E712
               LadderLeg.pick_date < lo)
        .order_by(LadderLeg.pick_date.asc())
    ).scalars().first()
    if prior:
        return prior

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
