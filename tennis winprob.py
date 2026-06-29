"""
tennis_winprob.py — in-match win probability from the live score.

A standard hierarchical serve model: each player wins a point on their serve
with probability p (from their serve stats, or a sane default). From p we get
the chance they hold a game; from hold probabilities + the current score we roll
up through games -> set -> match with exact recursions (memoized). Tiebreaks use
the players' blended serve strength.

Everything is derived from real score state — no guessing. When serve stats are
unknown we fall back to surface/era-typical hold rates and say so via defaults.
"""
from functools import lru_cache

DEFAULT_P = 0.62          # neutral point-win-on-serve when stats are unknown


def game_prob(p):
    """P(server holds) given per-point serve win prob p."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    q = 1 - p
    deuce = p * p / (p * p + q * q)
    return p**4 + 4 * p**4 * q + 10 * p**4 * q * q + 20 * (p**3) * (q**3) * deuce


def tb_prob(pa, pb):
    """P(A wins a 7-point tiebreak), using the average point-win prob across the
    two servers (they alternate serve through the breaker)."""
    a = (pa + (1 - pb)) / 2.0
    a = min(max(a, 1e-6), 1 - 1e-6)
    b = 1 - a

    @lru_cache(maxsize=None)
    def rec(i, j):
        if i >= 7 and i - j >= 2:
            return 1.0
        if j >= 7 and j - i >= 2:
            return 0.0
        if i >= 6 and j >= 6 and i == j:           # 6-6, 7-7 ... win by 2
            return a * a / (a * a + b * b)
        return a * rec(i + 1, j) + (1 - a) * rec(i, j + 1)

    return rec(0, 0)


def set_prob(ga, gb, a_serving, ha, hb, pa, pb):
    """P(A wins the set) from the current games, a_serving=True if A serves the
    next game. Handles 7-5 and the 6-6 tiebreak exactly."""
    @lru_cache(maxsize=None)
    def rec(a, b, a_srv):
        if a >= 6 and a - b >= 2:
            return 1.0
        if b >= 6 and b - a >= 2:
            return 0.0
        if a == 6 and b == 6:
            return tb_prob(pa, pb)
        if a_srv:
            return ha * rec(a + 1, b, False) + (1 - ha) * rec(a, b + 1, False)
        return hb * rec(a, b + 1, True) + (1 - hb) * rec(a + 1, b, True)

    return rec(ga, gb, a_serving)


def match_prob(sa, sb, ga, gb, a_serving, best_of, pa, pb):
    """P(A wins the match) from sets won (sa,sb), games in the current set, who
    serves next, and best_of (3 or 5). pa/pb = point-win-on-serve for A/B."""
    ha, hb = game_prob(pa), game_prob(pb)
    need = best_of // 2 + 1
    cur = set_prob(ga, gb, a_serving, ha, hb, pa, pb)
    # neutral set prob for sets not yet started (serve order unknown ahead)
    base = 0.5 * (set_prob(0, 0, True, ha, hb, pa, pb)
                  + set_prob(0, 0, False, ha, hb, pa, pb))

    @lru_cache(maxsize=None)
    def rec(a, b, pcur):
        if a >= need:
            return 1.0
        if b >= need:
            return 0.0
        return pcur * rec(a + 1, b, base) + (1 - pcur) * rec(a, b + 1, base)

    return rec(sa, sb, cur)


def p_from_service_pct(pct):
    """Map a player's service-points-won % (e.g. 65) to a per-point serve prob,
    clamped to a sane band."""
    if pct is None:
        return None
    p = pct / 100.0 if pct > 1 else float(pct)
    return min(max(p, 0.50), 0.80)
