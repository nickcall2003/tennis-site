"""
ats.py
------
Against-the-spread (ATS) tracking vs the OPENING line.

The metric, precisely: for each game we freeze the first spread we ever record
(the "opening line" — a true market open is a paid endpoint, so first-seen is our
opener, same convention the CLV code already uses). The model's PROJECTED MARGIN
picks a side relative to that opener; after the game we compare the ACTUAL MARGIN
to the same opener and record cover / no-cover / push.

All spreads and margins are HOME-RELATIVE: a spread of -6.5 means the home team is
favored by 6.5; a margin of +10 means the home team won by 10.

Deliberately its own table (ats_results) and its own metric — separate from the
straight-up accuracy record (PickResult), just like props are separate.

Everything is best-effort and must never raise into a request.
"""
from __future__ import annotations

import datetime as dt


def pick_side(model_margin, open_spread):
    """Which side the model takes ATS.

    The spread is home-relative points the home team is 'giving' (negative =
    home favored). The home team COVERS if actual_margin + open_spread > 0, i.e.
    if home wins by more than the spread. The model takes the home side when its
    projected margin beats the number the same way: model_margin + open_spread > 0.

    Returns 'home' or 'away'. On an exact tie (model projects exactly the number)
    we return None — no lean, so it isn't graded.
    """
    if model_margin is None or open_spread is None:
        return None
    edge = model_margin + open_spread          # >0: home covers per model; <0: away
    if edge > 0:
        return "home"
    if edge < 0:
        return "away"
    return None


def grade(open_spread, actual_margin, side):
    """Grade a finished game. Returns ('cover'|'no'|'push', covered_bool).

    home covers  when actual_margin + open_spread > 0
    away covers  when actual_margin + open_spread < 0
    push         when actual_margin + open_spread == 0 (landed on the number)
    """
    if open_spread is None or actual_margin is None or side not in ("home", "away"):
        return None, False
    diff = actual_margin + open_spread
    if diff == 0:
        return "push", False
    home_covered = diff > 0
    if side == "home":
        return ("cover", True) if home_covered else ("no", False)
    else:
        return ("cover", True) if not home_covered else ("no", False)


def record_ats(db, sport, ref, open_spread, model_margin, actual_margin,
               subcat=None, when=None):
    """Grade and persist one ATS result (idempotent per sport+ref). Returns the
    outcome dict, or None if it couldn't be graded (missing opener/margin/lean).
    Never raises."""
    from models import ATSResult
    side = pick_side(model_margin, open_spread)
    if side is None:
        return None
    result, covered = grade(open_spread, actual_margin, side)
    if result is None:
        return None
    when = when or dt.datetime.utcnow()
    try:
        existing = db.query(ATSResult).filter_by(sport=sport, ref=str(ref)).first()
        if existing:
            return {"sport": sport, "ref": str(ref), "result": existing.result,
                    "covered": existing.covered, "already": True}
        row = ATSResult(
            sport=sport, ref=str(ref), settled_date=when, subcat=subcat,
            open_spread=float(open_spread), model_margin=float(model_margin),
            actual_margin=float(actual_margin), pick_side=side,
            result=result, covered=bool(covered))
        db.add(row)
        db.commit()
        return {"sport": sport, "ref": str(ref), "result": result,
                "covered": covered, "side": side}
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[ats] record failed {sport}/{ref}: {e}")
        return None


def record(db, sport=None, days=None, subcat=None):
    """ATS record: {sport: {...}, overall: {...}}. Pushes are excluded from the
    win %, counted separately. Optionally filter to one sport / a recent window."""
    from models import ATSResult
    q = db.query(ATSResult)
    if sport:
        q = q.filter(ATSResult.sport == sport)
    if subcat:
        q = q.filter(ATSResult.subcat == subcat)
    if days:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=days)
        q = q.filter(ATSResult.settled_date >= cutoff)
    rows = q.all()

    def _summ(rs):
        cov = sum(1 for r in rs if r.result == "cover")
        no = sum(1 for r in rs if r.result == "no")
        push = sum(1 for r in rs if r.result == "push")
        graded = cov + no                      # pushes excluded from win%
        return {
            "covers": cov, "no_covers": no, "pushes": push,
            "graded": graded,
            "ats_pct": round(100 * cov / graded, 1) if graded else None,
            "record": f"{cov}-{no}" + (f"-{push}" if push else ""),
        }

    by = {}
    for r in rows:
        by.setdefault(r.sport, []).append(r)
    out = {"by_sport": {sp: _summ(rs) for sp, rs in by.items()},
           "overall": _summ(rows)}
    out["overall"]["note"] = ("ATS vs the opening line (first spread recorded). "
                              "Pushes excluded from win %. Its own metric, "
                              "separate from straight-up accuracy.")
    return out
