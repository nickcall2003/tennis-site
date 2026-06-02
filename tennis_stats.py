"""
tennis_stats.py
---------------
Compute match statistics from API-Tennis "pointbypoint" data.

WHAT THE FEED GIVES US: a list of games, each with who served and a list of
points (with the running score and break-point flags). From the sequence of
point scores we can infer who won each point, and from there derive the stats
that are mathematically recoverable.

WHAT WE CAN COMPUTE (real numbers):
  - Service points won           (points won while serving)
  - Return points won            (points won while receiving)
  - Total points won
  - Service games won / Return games won / Total games won
  - Break points saved           (BP faced on serve that the server won)
  - Break points converted       (BP the returner won -> a break)

WHAT THIS FEED CANNOT GIVE (shown as "—" on the page):
  - Aces, Double Faults          (points aren't tagged as ace/DF)
  - 1st serve %, 1st/2nd serve points won, 1st/2nd return points won
    (points aren't tagged first- vs second-serve)
  - Winners, Unforced errors, Net points (no shot-outcome data)
These require a higher-tier statistics feed (e.g. Sportradar/SportsDataIO).

Everything here is defensive: malformed or missing data yields an empty result
rather than an exception, so the page degrades gracefully.
"""

from __future__ import annotations

# Players are "First Player" / "Second Player" in the feed; we map to a / b.
_FIRST = "First Player"
_SECOND = "Second Player"

# Point scores in the feed read as "first - second" (per API-Tennis docs), so a=first.
# If a real live match ever shows the two players' stats SWAPPED, flip this to True,
# re-upload tennis_stats.py, and the orientation corrects itself. (One-line change.)
SWAP_PLAYERS = False


def _score_pair(score: str):
    """'30 - 15' -> (30,15) using a point ladder index. Returns None on junk."""
    ladder = {"0": 0, "15": 1, "30": 2, "40": 3, "A": 4, "AD": 4, "Ad": 4}
    if not score or "-" not in score:
        return None
    left, _, right = score.partition("-")
    l, r = left.strip(), right.strip()
    if l in ladder and r in ladder:
        return ladder[l], ladder[r]
    # Tiebreak points are plain integers ("5 - 3")
    if l.isdigit() and r.isdigit():
        return int(l), int(r)
    return None


def _point_winners(points: list) -> list[str]:
    """
    Infer who won each point in a game from the running score.
    Score is read as 'First - Second' (absolute, not server-relative).
    Returns a list of 'a'/'b' per point.
    """
    winners = []
    prev = (0, 0)
    for p in points:
        cur = _score_pair(p.get("score", ""))
        if cur is None:
            continue
        # Whoever's tally went up won this point. On resets/deuce dips we fall
        # back to "no change recorded" and skip.
        da, db = cur[0] - prev[0], cur[1] - prev[1]
        if da > 0 and da >= db:
            winners.append("a")
        elif db > 0 and db > da:
            winners.append("b")
        prev = cur
    return winners


def compute_stats(pointbypoint: list) -> dict | None:
    """
    Turn a pointbypoint list into a stats dict for both players (a/b).
    Returns None if there's nothing usable.
    """
    if not pointbypoint:
        return None

    sp_won = {"a": 0, "b": 0}      # service points won
    sp_tot = {"a": 0, "b": 0}      # service points total (points served)
    rp_won = {"a": 0, "b": 0}      # return points won
    rp_tot = {"a": 0, "b": 0}      # return points total (points received)
    games_won = {"a": 0, "b": 0}
    serv_games_won = {"a": 0, "b": 0}
    serv_games_tot = {"a": 0, "b": 0}
    ret_games_won = {"a": 0, "b": 0}
    bp_saved = {"a": 0, "b": 0}    # server saved a break point
    bp_faced = {"a": 0, "b": 0}    # server faced a break point
    bp_won = {"a": 0, "b": 0}      # returner converted a break point
    bp_chances = {"a": 0, "b": 0}  # returner's break chances

    any_points = False

    for game in pointbypoint:
        server = "a" if game.get("player_served") == _FIRST else \
                 "b" if game.get("player_served") == _SECOND else None
        if server is None:
            continue
        returner = "b" if server == "a" else "a"
        points = game.get("points") or []
        winners = _point_winners(points)
        if not winners:
            continue
        any_points = True

        serv_games_tot[server] += 1

        for i, w in enumerate(winners):
            # service vs return point
            if w == server:
                sp_won[server] += 1
            else:
                rp_won[returner] += 1
            sp_tot[server] += 1
            rp_tot[returner] += 1

            # break-point flag on the raw point (returner is threatening)
            raw = points[i] if i < len(points) else {}
            if raw.get("break_point"):
                bp_faced[server] += 1
                bp_chances[returner] += 1
                if w == server:
                    bp_saved[server] += 1
                else:
                    bp_won[returner] += 1

        # who won the game = winner of the last point
        gw = winners[-1]
        games_won[gw] += 1
        if gw == server:
            serv_games_won[server] += 1
        else:
            ret_games_won[returner] += 1

    if not any_points:
        return None

    def pct(n, d):
        return round(100 * n / d) if d else None

    def block(side):
        return {
            "service_points_won": sp_won[side],
            "service_points_total": sp_tot[side],
            "service_points_pct": pct(sp_won[side], sp_tot[side]),
            "return_points_won": rp_won[side],
            "return_points_total": rp_tot[side],
            "return_points_pct": pct(rp_won[side], rp_tot[side]),
            "total_points_won": sp_won[side] + rp_won[side],
            "service_games_won": serv_games_won[side],
            "service_games_total": serv_games_tot[side],
            "service_games_pct": pct(serv_games_won[side], serv_games_tot[side]),
            "return_games_won": ret_games_won[side],
            "games_won": games_won[side],
            "break_points_saved": bp_saved[side],
            "break_points_faced": bp_faced[side],
            "break_points_won": bp_won[side],
            "break_points_chances": bp_chances[side],
            # Not derivable from this feed:
            "aces": None, "double_faults": None, "first_serve_pct": None,
            "first_serve_points_won": None, "second_serve_points_won": None,
            "winners": None, "unforced_errors": None,
        }

    total_pts = sp_won["a"] + rp_won["a"] + sp_won["b"] + rp_won["b"]
    total_games = games_won["a"] + games_won["b"]
    A, B = ("b", "a") if SWAP_PLAYERS else ("a", "b")
    return {
        "a": block(A),
        "b": block(B),
        "total_points": total_pts,
        "total_games": total_games,
        "has_data": total_pts > 0,
    }
