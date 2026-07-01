"""
calibrate.py — turns the model's raw probabilities into HONEST probabilities and
computes edge against a DE-VIGGED market line.

Why this exists: the raw model is overconfident, so raw "edge" (raw_prob minus the
market's vig-inflated implied prob) is dominated by overconfidence error — the
bigger the claimed edge, the bigger the mistake, which is why high-edge picks were
losing. We fix that two ways:

  1. Platt-scale each sport's probabilities against its own settled results, so a
     stated 65% really means 65%. Fit is shrunk toward a conservative default when
     a sport has thin data, so we never overfit a handful of games.
  2. De-vig the market (use both sides' prices) so edge is measured against the
     market's TRUE probability, not the overround-inflated single-side number.

edge = calibrated_prob - devigged_market_prob. Only that is trustworthy.
"""
import math
import time

_sig_cache = {}
_TTL = 1800                     # refit at most every 30 min per sport
_DEFAULT_A, _DEFAULT_B = 0.80, 0.0   # mild overconfidence correction when data is thin
_MIN_N = 60                     # need this many graded picks before trusting a fit
_FULL_TRUST_N = 300             # fit fully trusted at/above this many


def _sig(x):
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _logit(p):
    p = min(0.999, max(0.001, float(p)))
    return math.log(p / (1.0 - p))


def _imp(o):
    o = float(o)
    return (100.0 / (o + 100.0)) if o > 0 else (abs(o) / (abs(o) + 100.0))


def fit_platt(data, iters=600, lr=0.3):
    """Fit won ~ sigmoid(a*logit(p)+b) by gradient descent. a<1 corrects
    overconfidence (pulls probabilities toward 0.5)."""
    a, b = 1.0, 0.0
    n = len(data)
    if n == 0:
        return 1.0, 0.0
    for _ in range(iters):
        ga = gb = 0.0
        for p, won in data:
            x = _logit(p)
            e = _sig(a * x + b) - (1.0 if won else 0.0)
            ga += e * x
            gb += e
        a -= lr * ga / n
        b -= lr * gb / n
    if not (0.2 <= a <= 1.5) or math.isnan(a) or math.isnan(b):
        return _DEFAULT_A, _DEFAULT_B
    return a, b


def calib_params(sport):
    """(a, b, n) for a sport, cached. Fit from settled PickResult rows that carry a
    model prob; shrunk toward the conservative default by sample size."""
    now = time.time()
    c = _sig_cache.get(sport)
    if c and c[0] > now:
        return c[1], c[2], c[3]
    a, b, n = _DEFAULT_A, _DEFAULT_B, 0
    try:
        from db import SessionLocal
        from models import PickResult
        with SessionLocal() as db:
            rows = db.query(PickResult).filter(
                PickResult.sport == sport,
                PickResult.prob.isnot(None)).all()
        data = [(float(r.prob), bool(r.correct))
                for r in rows if r.prob is not None and 0.0 < r.prob < 1.0]
        n = len(data)
        if n >= _MIN_N:
            fa, fb = fit_platt(data)
            w = min(1.0, n / float(_FULL_TRUST_N))   # shrink fit toward default on thin data
            a = w * fa + (1.0 - w) * _DEFAULT_A
            b = w * fb
    except Exception:
        pass
    _sig_cache[sport] = (now + _TTL, a, b, n)
    return a, b, n


def calibrate(sport, p):
    """Raw model prob -> calibrated (honest) prob for a sport."""
    if p is None:
        return p
    try:
        a, b, _ = calib_params(sport)
        return _sig(a * _logit(p) + b)
    except Exception:
        return p


def devig(o_side, o_other):
    """De-vigged probability for o_side given both American prices. Falls back to
    the raw single-side implied prob if the other side is missing."""
    try:
        if o_other is None:
            return _imp(o_side)
        a, b = _imp(o_side), _imp(o_other)
        s = a + b
        return a / s if s > 0 else a
    except Exception:
        return None


def edge(sport, raw_prob, side_odds, other_odds=None):
    """The only trustworthy edge: calibrated model prob minus de-vigged market
    prob, as a fraction (0.04 = 4%). Returns None if it can't be computed."""
    try:
        cp = calibrate(sport, raw_prob)
        mp = devig(side_odds, other_odds)
        if cp is None or mp is None:
            return None
        return cp - mp
    except Exception:
        return None
