"""
prop_tracking.py — persist player props and grade them once the game is final.

Purpose: see whether the PROJECTION MODEL is actually any good. Every prop the
model shows is logged with its projection, the book line, and the lean; after the
game we fill in what the player actually did and mark the lean right or wrong.

IMPORTANT: this is a MODEL-QUALITY scoreboard only. Prop results deliberately do
NOT feed the site's win/loss record, units, ROI, or CLV — those track the game
picks. Keeping them separate means a hot or cold prop stretch can't distort the
betting record (and vice versa).

Nothing here is allowed to break a page: every function is best-effort.
"""
import datetime as dt

from sqlalchemy import select

from models import PropResult


# ---------- record (when props are shown) ----------
def log_props(db, sport, game_ref, props, when=None):
    """Upsert the props we showed for a game (projection/line/lean), ungraded."""
    if not props:
        return 0
    when = when or dt.datetime.now()
    n = 0
    for p in props:
        try:
            player = (p.get("player") or "").strip()
            stat = (p.get("stat") or p.get("label") or "").strip()
            line, proj = p.get("line"), p.get("projection")
            if not player or not stat or line is None or proj is None:
                continue
            lean = "OVER" if float(proj) > float(line) else "UNDER"
            odds = p.get("over_odds") if lean == "OVER" else p.get("under_odds")
            existing = db.execute(
                select(PropResult).where(
                    PropResult.sport == sport, PropResult.game_ref == str(game_ref),
                    PropResult.player == player, PropResult.stat == stat)
            ).scalar_one_or_none()
            if existing:
                if existing.actual is None:      # not graded yet -> refresh the numbers
                    existing.line = float(line)
                    existing.projection = float(proj)
                    existing.lean = lean
                    existing.odds = odds
                continue
            db.add(PropResult(
                sport=sport, game_ref=str(game_ref), settled_date=when,
                player=player, stat=stat, line=float(line), projection=float(proj),
                lean=lean, odds=odds, actual=None, correct=None))
            n += 1
        except Exception:
            continue
    if n:
        db.commit()
    return n


# ---------- grade (after the game) ----------
def grade_props(db, sport, game_ref, actuals, when=None):
    """actuals: {(player_lower, stat_lower): value}. Fills in actual + correct.
    A result exactly ON the line is a push (correct = None)."""
    if not actuals:
        return 0
    rows = db.execute(
        select(PropResult).where(
            PropResult.sport == sport, PropResult.game_ref == str(game_ref),
            PropResult.actual.is_(None))
    ).scalars().all()
    n = 0
    for r in rows:
        val = actuals.get((r.player.lower(), r.stat.lower()))
        if val is None:
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        r.actual = val
        if val == r.line:
            r.correct = None                       # push
        else:
            went_over = val > r.line
            r.correct = (went_over and r.lean == "OVER") or ((not went_over) and r.lean == "UNDER")
        if when:
            r.settled_date = when
        n += 1
    if n:
        db.commit()
    return n


# ---------- read (the scoreboard) ----------
def prop_record(db, sport=None, days=30):
    """How the projection model is doing: hit rate by stat, and overall."""
    since = dt.datetime.now() - dt.timedelta(days=days)
    q = select(PropResult).where(PropResult.settled_date >= since,
                                 PropResult.actual.isnot(None))
    if sport:
        q = q.where(PropResult.sport == sport)
    rows = db.execute(q).scalars().all()
    graded = [r for r in rows if r.correct is not None]
    by_stat = {}
    for r in graded:
        b = by_stat.setdefault(r.stat, {"w": 0, "l": 0})
        b["w" if r.correct else "l"] += 1
    for b in by_stat.values():
        t = b["w"] + b["l"]
        b["hit_pct"] = round(100 * b["w"] / t, 1) if t else None
        b["n"] = t
    w = sum(1 for r in graded if r.correct)
    total = len(graded)
    # average absolute projection error — the honest "how close is the model"
    errs = [abs(r.actual - r.projection) for r in graded if r.actual is not None]
    return {
        "sport": sport or "all", "days": days,
        "graded": total, "wins": w, "losses": total - w,
        "hit_pct": round(100 * w / total, 1) if total else None,
        "avg_abs_error": round(sum(errs) / len(errs), 2) if errs else None,
        "pushes": sum(1 for r in rows if r.correct is None and r.actual is not None),
        "by_stat": by_stat,
        "note": ("Model-quality tracking only \u2014 prop results do NOT count toward the "
                 "site's win/loss record, units, or ROI."),
    }


def recent_props(db, sport=None, limit=60):
    """Recently graded props (for a results view)."""
    q = select(PropResult).where(PropResult.actual.isnot(None))
    if sport:
        q = q.where(PropResult.sport == sport)
    rows = db.execute(q.order_by(PropResult.settled_date.desc()).limit(limit)).scalars().all()
    return [{
        "sport": r.sport, "player": r.player, "stat": r.stat,
        "line": r.line, "projection": r.projection, "lean": r.lean,
        "actual": r.actual,
        "result": ("push" if r.correct is None else ("win" if r.correct else "loss")),
        "date": r.settled_date.date().isoformat() if r.settled_date else None,
    } for r in rows]
