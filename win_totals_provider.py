"""
win_totals_provider.py — assemble a team's season win-total projection from the
ESPN schedule + our Elo ratings, then hand it to win_totals.project().

WHY A DEDICATED SCHEDULE FETCH
espn_provider._team_schedule already exists but returns only COMPLETED games
(it's built for head-to-head history) and drops opponent name/venue. Win totals
need the FULL schedule — played and upcoming — with home/away and each
opponent's id so we can attach a win probability. So this pulls the same ESPN
schedule endpoint but keeps every event.

WIN PROBABILITY PER GAME
Standard Elo logistic on the rating gap, with home-court added the same way the
live model does it, so a projected game and a live-board game use identical
math. If either team's rating is missing, that game falls back to a coin flip
(0.5) rather than guessing — and win_totals.project() surfaces how many games
that affected via games without ratings, so a projection built on thin data is
visibly thin.
"""
from __future__ import annotations

import os

import win_totals

# Home edge in Elo points, per sport. These mirror the live model's values;
# tune per sport if the live board uses different ones.
_HOME_ELO = {
    "nfl": 55, "ncaaf": 65, "nba": 70, "ncaab": 75, "nhl": 50, "mlb": 40,
}

# ESPN scoreboard bases, borrowed from espn_provider's registry when available.
_BASE = {
    "nfl":   "https://site.api.espn.com/apis/site/v2/sports/football/nfl",
    "ncaaf": "https://site.api.espn.com/apis/site/v2/sports/football/college-football",
    "nba":   "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
    "ncaab": "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball",
    "nhl":   "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl",
    "mlb":   "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb",
}


def _prob_to_elo(p):
    """Invert the logistic: a win probability -> its Elo-difference equivalent.
    Same conversion the live model uses so adjustments compose identically."""
    import math
    p = min(0.999, max(0.001, p))
    return -400.0 * math.log10(1.0 / p - 1.0)


def _win_prob(sport, team_rating, opp_rating, home, inj_pts=0.0):
    """Elo logistic, home edge folded in, plus an optional injury adjustment in
    Elo points (positive = favours this team). Falls back to 0.5 when a rating
    is missing so a thin projection is honest rather than invented.

    inj_pts is applied in Elo space exactly like espn_provider does on the live
    board, so a projected game and a live game use identical math."""
    if team_rating is None or opp_rating is None:
        # even with no ratings, an injury signal is better than a blind coin flip
        if inj_pts:
            base = _prob_to_elo(0.5)
            return 1.0 / (1.0 + 10 ** (-((base + inj_pts) / 400.0))), True
        return 0.5, True
    edge = _HOME_ELO.get(sport, 55)
    diff = (team_rating - opp_rating) + (edge if home else -edge) + inj_pts
    return 1.0 / (1.0 + 10 ** (-diff / 400.0)), False


def _injury_pts(sport, team_id, opp_id):
    """Net injury swing in Elo points for THIS team vs its opponent, using the
    same injuries module the live board uses. Only meaningfully affects unplayed
    games near game time (ESPN reports current injuries), so it sharpens the
    remaining schedule without rewriting history."""
    try:
        import injuries as _inj
        if not _inj.enabled(sport):
            return 0.0, None
        me = _inj.for_team(sport, team_id) or {}
        opp = _inj.for_team(sport, opp_id) or {}
        # positive favours this team: our opponent's penalty minus our own
        net = (opp.get("penalty", 0.0) - me.get("penalty", 0.0))
        players = me.get("players", [])
        return net, (players or None)
    except Exception:
        return 0.0, None


def _full_schedule(sport, team_id, season=None):
    """Every event on a team's ESPN schedule — played and upcoming — with
    opponent id/name, home/away, and result if final."""
    import espn_provider as EP
    base = _BASE.get(sport)
    if not base:
        return []
    try:
        data = EP._get(f"{base}/teams/{team_id}/schedule",
                       {} if season is None else {"season": season})
    except Exception:
        return []
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        me = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        opp = next((c for c in cs if str((c.get("team") or {}).get("id")) != str(team_id)), None)
        if not me or not opp:
            continue
        status = ((comp.get("status") or {}).get("type") or {})
        completed = bool(status.get("completed"))
        out.append({
            "opp_id": str((opp.get("team") or {}).get("id")),
            "opp_name": (opp.get("team") or {}).get("displayName")
                        or (opp.get("team") or {}).get("abbreviation") or "Opponent",
            "home": (me.get("homeAway") == "home"),
            "date": ev.get("date"),
            "completed": completed,
            "won": (me.get("winner") is True) if completed else None,
        })
    return out


def league_teams(sport):
    """All teams in a league: [{team_id, name, abbr, logo}]. One cached fetch
    per sport — used by the win-totals page to enumerate the league."""
    import espn_provider as EP
    base = _BASE.get(sport)
    if not base:
        return []
    key = f"_wt_teams_{sport}"
    cached = globals().get(key)
    if cached is not None:
        return cached
    out = []
    try:
        data = EP._get(f"{base}/teams")
        groups = ((data.get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams") or []
        for wrap in groups:
            t = wrap.get("team") or {}
            logos = t.get("logos") or []
            out.append({
                "team_id": str(t.get("id")),
                "name": t.get("displayName") or t.get("name") or "Team",
                "abbr": t.get("abbreviation") or "",
                "logo": (logos[0].get("href") if logos else None),
            })
    except Exception:
        out = []
    globals()[key] = out
    return out


def project_team(sport, team_id, team_name, line=None, season=None):
    """Full win-total projection for one team, with injury and strength-of-
    schedule context layered on top of the base rating model."""
    import espn_elo
    sched = _full_schedule(sport, team_id, season)
    if not sched:
        return None
    my_rating = espn_elo.lookup(sport, team_id)
    games = []
    opp_ratings = []
    missing_ratings = 0
    injuries_applied = 0
    for g in sched:
        opp_rating = espn_elo.lookup(sport, g["opp_id"])
        if opp_rating is not None:
            opp_ratings.append(opp_rating)
        # injuries only matter for games not yet played — don't rewrite results
        inj_pts, inj_players = (0.0, None)
        if not g["completed"]:
            inj_pts, inj_players = _injury_pts(sport, team_id, g["opp_id"])
            if inj_pts:
                injuries_applied += 1
        p, missing = _win_prob(sport, my_rating, opp_rating, g["home"], inj_pts)
        if missing:
            missing_ratings += 1
        games.append(win_totals.Game(
            opponent=g["opp_name"], win_prob=p, home=g["home"],
            played=g["completed"], won=g["won"], date=g["date"]))
    proj = win_totals.project(team_name, games, line=line)
    out = win_totals.to_dict(proj)

    # strength of schedule: average opponent rating vs the league baseline (1500).
    # Positive = tougher-than-average slate, which contextualises the projection.
    if opp_ratings:
        avg_opp = sum(opp_ratings) / len(opp_ratings)
        out["sos"] = round(avg_opp - 1500.0, 1)
        out["sos_rank_hint"] = ("hard" if avg_opp - 1500 >= 40 else
                                "soft" if avg_opp - 1500 <= -40 else "average")
    else:
        out["sos"] = None
        out["sos_rank_hint"] = None
    # honesty flags: how much of this projection rests on real data
    out["rating"] = round(my_rating, 1) if my_rating is not None else None
    out["games_no_rating"] = missing_ratings
    out["injuries_applied"] = injuries_applied
    out["model"] = "elo+injury+sos"
    return out


def project_league(sport, teams=None, lines=None):
    """teams: list of {team_id, name}. If omitted, projects the whole league.
    lines: optional {team_id: win_total}. Returns projections sorted by biggest
    over/under edge first — the teams where the model most disagrees with the
    book (or, with no lines, the strongest/weakest projected records)."""
    lines = lines or {}
    if teams is None:
        teams = league_teams(sport)
    out = []
    for t in teams:
        tid = str(t.get("team_id"))
        pr = project_team(sport, tid, t.get("name") or tid,
                          line=lines.get(tid), season=t.get("season"))
        if pr:
            out.append(pr)
    if any(p.get("edge_wins") is not None for p in out):
        # lines present -> rank by disagreement with the book
        out.sort(key=lambda p: abs(p["edge_wins"]) if p.get("edge_wins") is not None else -1,
                 reverse=True)
    else:
        # projection-only -> rank by projected wins, best season first
        out.sort(key=lambda p: p["expected_wins"], reverse=True)
    return {"sport": sport, "count": len(out), "teams": out,
            "has_lines": any(p.get("line") is not None for p in out)}
