"""
props.py
--------
Player-prop projections. A prop is: given a player's expected output for a stat
(adjusted for the matchup) and its game-to-game variability, what's the
probability they go OVER a given line?

Two distribution choices, picked by stat type:
  - COUNTING stats that are low and discrete (strikeouts, HR, made threes) ->
    Poisson around the projected mean. P(over L) = P(X > floor(L)).
  - CONTINUOUS / higher-volume stats (points, yards, rebounds) -> Normal around
    the mean with a sport/stat-specific standard deviation.

The projection itself is: base_rate * matchup_factor, where matchup_factor is
1.0 when we have no opponent context (so it degrades gracefully to the player's
own season pace). Everything is clamped to stay sane.

This is honest sports modeling, not a guarantee — props are high-variance by
nature. We always show the projection AND the line so the user decides.
"""

from __future__ import annotations

import math

# Typical game-to-game standard deviation as a fraction of the mean, by stat.
# (Rough, literature-informed values; tune later against results.)
_NORMAL_CV = {
    "points": 0.33, "rebounds": 0.42, "assists": 0.45,
    "passing_yards": 0.30, "rushing_yards": 0.55, "receiving_yards": 0.55,
    "default": 0.40,
}
_POISSON_STATS = {"strikeouts", "made_threes", "home_runs", "hits", "rbis", "total_bases"}


def _poisson_sf(line, mean):
    """P(X > line) for X ~ Poisson(mean). Over a half-line, > floor(line)."""
    k = math.floor(line)
    # P(X > k) = 1 - CDF(k)
    cdf = 0.0
    term = math.exp(-mean)
    cdf += term
    for i in range(1, k + 1):
        term *= mean / i
        cdf += term
    return max(0.0, min(1.0, 1.0 - cdf))


def _normal_sf(line, mean, sd):
    if sd <= 0:
        return 1.0 if mean > line else 0.0
    z = (line - mean) / sd
    return 1.0 - 0.5 * (1 + math.erf(z / math.sqrt(2)))


def project_prop(stat, base_rate, line, matchup_factor=1.0, minutes_factor=1.0):
    """
    Returns a dict: projection, over/under probability, and a lean.
      stat           - key like 'points', 'strikeouts', 'passing_yards'
      base_rate      - player's season per-game average for the stat
      line           - the over/under number (e.g. 4.5)
      matchup_factor - >1 if the matchup boosts this stat (weak opponent), <1 if tough
      minutes_factor - playing-time adjustment (e.g. expected role change)
    """
    if base_rate is None or base_rate <= 0:
        return None
    proj = base_rate * matchup_factor * minutes_factor
    if stat in _POISSON_STATS:
        p_over = _poisson_sf(line, proj)
    else:
        cv = _NORMAL_CV.get(stat, _NORMAL_CV["default"])
        p_over = _normal_sf(line, proj, proj * cv)
    p_over = max(0.02, min(0.98, p_over))
    return {
        "stat": stat,
        "projection": round(proj, 1),
        "line": line,
        "over_prob": round(p_over, 3),
        "under_prob": round(1 - p_over, 3),
        "lean": "over" if p_over >= 0.5 else "under",
        "edge": round(abs(p_over - 0.5) * 2, 3),    # 0..1 confidence-ish
    }


def default_line(stat, base_rate):
    """A sensible line when no sportsbook line is supplied: round near the mean."""
    if base_rate is None:
        return None
    if stat in _POISSON_STATS:
        return math.floor(base_rate) + 0.5
    if base_rate < 40:                    # points, rebounds, assists
        return round(base_rate - 0.5) + 0.5
    return round(base_rate / 5) * 5 - 0.5  # yards -> x49.5 / x9.5 style lines
