"""
mlb_calibration.py — make the MLB model's stated confidence match reality.

The diagnostic showed the model averaging ~63% win probability while actually
hitting ~57%: its probabilities are inflated by 5-6 points. This shrinks each
probability toward 0.5 by a factor LEARNED from the model's own settled results,
so the average stated confidence lines up with the realized hit rate.

Important: shrinking toward 0.5 never flips which side the model prefers (a 0.63
stays above 0.5), so this changes CONFIDENCE, not the pick. It also naturally
tempers false "edges" — inflated probs manufacture edges that aren't real, and an
honest prob bets less. Self-updating (recomputed from pick_results a couple times
a day); a no-op until there's enough data. Disable with MLB_CALIBRATION=0.
"""
import os
import time

_cache = {"ts": 0.0, "k": 1.0, "n": 0}
_TTL = 12 * 3600
_MIN_SAMPLE = int(os.environ.get("MLB_CAL_MIN", "150"))


def _fresh_factor():
    """Learn the shrink k: (actual_edge / model_edge) over a rolling window, where
    edge is distance from 0.5. k=1 means already well-calibrated; k<1 means the
    model is overconfident and probs get pulled toward 0.5. Returns (k, n)."""
    try:
        import datetime as dt
        from db import SessionLocal
        from models import PickResult
        days = int(os.environ.get("MLB_CAL_DAYS", "120"))
        since = dt.datetime.now() - dt.timedelta(days=days)
        with SessionLocal() as db:
            rows = (db.query(PickResult)
                      .filter(PickResult.sport == "mlb",
                              PickResult.settled_date >= since,
                              PickResult.prob.isnot(None)).all())
        n = len(rows)
        if n < _MIN_SAMPLE:
            return 1.0, n
        avg_p = sum(r.prob for r in rows) / n
        win_rate = sum(1 for r in rows if r.correct) / n
        model_edge = avg_p - 0.5
        if model_edge <= 0.005:
            return 1.0, n
        k = (win_rate - 0.5) / model_edge
        return max(0.0, min(1.0, k)), n
    except Exception:
        return 1.0, 0


def factor():
    now = time.time()
    if now - _cache["ts"] > _TTL:
        k, n = _fresh_factor()
        _cache.update(ts=now, k=k, n=n)
    return _cache["k"]


def info():
    factor()
    return dict(_cache)


def calibrate(p):
    """Shrink a model win probability toward 0.5 by the learned factor so stated
    confidence matches realized hit rate. No-op when disabled or under-sampled."""
    if p is None or os.environ.get("MLB_CALIBRATION", "1") != "1":
        return p
    try:
        return 0.5 + (float(p) - 0.5) * factor()
    except (TypeError, ValueError):
        return p
