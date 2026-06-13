"""
sports.py — the single registry of every sport the site supports.

WHY THIS EXISTS
---------------
Before this, each sport was repeated across main.py: a branch in team_games,
another in team_game, another in the picks gather, another in the writeups,
plus hardcoded label/color/emoji in the frontend. Adding a sport meant editing
all of them and hoping you didn't miss one. This registry declares each sport
ONCE; the routes, picks, and writeups read from here and loop instead of
branching.

THE "HYBRID" MODEL
------------------
- Relational/persisted data (tennis matches, PickResult/PickLog/OddsSnapshot)
  lives in the SQLAlchemy ORM (db.py / models.py).
- Slow-moving team stats live in per-sport JSON files (the providers read them).
- This registry ties each sport's provider + display metadata + flags together
  so the rest of the app is data-driven, not branch-driven.

Every team sport exposes a uniform interface:
    games(date)            -> list[game dict]   (same shape across sports)
    game(date, game_id)    -> game dict | None
Game dicts already share one shape (id, sport, status, home, away, prob_home,
exp_margin, confidence, avg_total, factors, venue, score, winner, event_time),
which is exactly what made this generalization possible.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
import datetime as dt


# ---- per-sport game adapters (lazy imports: no import-time cost or cycles) ----

def _espn_games(sport):
    def f(date):
        from espn_provider import get_games
        return get_games(sport, date) or []
    return f


def _espn_game(sport):
    def f(date, gid):
        from espn_provider import get_game
        return get_game(sport, date, gid)
    return f


def _mlb_games(date):
    from mlb_provider import get_games
    return get_games(date) or []


def _ncaabb_games(date):
    # Dual-source, mirroring the dedicated route: Highlightly first (if enabled),
    # else ESPN backbone. ESPN is the reliable source; Highlightly is enrichment.
    try:
        import highlightly as hl
        if hl.enabled():
            g = hl.get_games(date) or []
            if g:
                return g
    except Exception:
        pass
    try:
        from ncaab_baseball import get_games as eg
        return eg(date) or []
    except Exception:
        return []


def _nhl_games(date):
    from nhl_games import get_games
    return get_games(date) or []


def _soccer_games(date):
    # registry default (EPL); the real board uses the league-aware route
    from soccer_provider import get_games
    return get_games(date) or []


def _find_by_id(games_fn):
    def f(date, gid):
        for g in games_fn(date):
            if str(g.get("id")) == str(gid):
                return g
        return None
    return f


@dataclass(frozen=True)
class Sport:
    key: str
    label: str
    emoji: str
    color: str
    kind: str                          # "tennis" | "mlb" | "espn" | "team"
    source: str = ""                   # human-readable data source
    has_props: bool = False
    blurb: str = ""                     # short tile subtitle for the home page
    stat_file: Optional[str] = None    # the file-backed stat cache, if any
    games: Optional[Callable] = None   # (date) -> list[game dict]
    game: Optional[Callable] = None    # (date, id) -> game dict | None

    @property
    def is_team(self) -> bool:
        return self.kind in ("mlb", "espn", "team", "soccer")

    @property
    def generic_team(self) -> bool:
        # served by the shared /api/{sport}/games team route (not bespoke mlb/tennis)
        return self.kind in ("espn", "team")


# ----------------------------- THE REGISTRY --------------------------------
SPORTS: dict[str, Sport] = {
    "tennis": Sport(
        key="tennis", label="Tennis", emoji="\U0001F3BE", color="#4f9d6a",
        kind="tennis", source="API-Tennis + Elo model", blurb="ATP \u00b7 WTA \u00b7 Challenger"),
    "mlb": Sport(
        key="mlb", label="MLB", emoji="\u26BE", color="#3f7fc4",
        kind="mlb", source="ESPN + run-expectancy model", has_props=True,
        blurb="Run lines \u00b7 props", games=_mlb_games),
    "nba": Sport(
        key="nba", label="NBA", emoji="\U0001F3C0", color="#c8612f",
        kind="espn", source="ESPN + Elo model", has_props=True, blurb="Spreads \u00b7 player props",
        games=_espn_games("nba"), game=_espn_game("nba")),
    "nfl": Sport(
        key="nfl", label="NFL", emoji="\U0001F3C8", color="#4f9d6a",
        kind="espn", source="ESPN + Elo model", has_props=True, blurb="Spreads \u00b7 yardage props",
        games=_espn_games("nfl"), game=_espn_game("nfl")),
    "ncaabb": Sport(
        key="ncaabb", label="NCAA Baseball", emoji="\U0001F9E2", color="#7b9e3a",
        kind="team", source="ESPN + Warren Nolan RPI + run-expectancy", blurb="RPI-aware \u00b7 D1",
        stat_file="ncaa_stats.json",
        games=_ncaabb_games, game=_find_by_id(_ncaabb_games)),
    "nhl": Sport(
        key="nhl", label="NHL", emoji="\U0001F3D2", color="#5a6b8c",
        kind="team", source="ESPN + Poisson xG model", blurb="xG model \u00b7 32 teams",
        stat_file="nhl_stats.json",
        games=_nhl_games, game=_find_by_id(_nhl_games)),
    "ncaaf": Sport(
        key="ncaaf", label="NCAA Football", emoji="\U0001F3C8", color="#b5651d",
        kind="espn", source="ESPN + team-strength model", blurb="FBS \u00b7 AP-aware",
        games=_espn_games("ncaaf"), game=_espn_game("ncaaf")),
    "ncaab": Sport(
        key="ncaab", label="NCAA Basketball", emoji="\U0001F3C0", color="#d08c3f",
        kind="espn", source="ESPN + team-strength model", blurb="D1 men \u00b7 AP-aware",
        games=_espn_games("ncaab"), game=_espn_game("ncaab")),
    "wncaab": Sport(
        key="wncaab", label="Women's CBB", emoji="\U0001F3C0", color="#c0567e",
        kind="espn", source="ESPN + team-strength model", blurb="D1 women \u00b7 AP-aware",
        games=_espn_games("wncaab"), game=_espn_game("wncaab")),
    "soccer": Sport(
        key="soccer", label="Soccer", emoji="\u26BD", color="#2f9e6f",
        kind="soccer", source="ESPN (multi-league) + Poisson model",
        blurb="EPL \u00b7 UCL \u00b7 La Liga \u00b7 MLS \u2026",
        games=_soccer_games, game=_find_by_id(_soccer_games)),
}

# Convenience views used by the routes/picks loops.
ALL_KEYS = list(SPORTS.keys())
TEAM_KEYS = [k for k, s in SPORTS.items() if s.is_team]                 # mlb,nba,nfl,ncaabb,nhl
GENERIC_TEAM_KEYS = [k for k, s in SPORTS.items() if s.generic_team]    # nba,nfl,ncaabb,nhl


def get(key: str) -> Optional[Sport]:
    return SPORTS.get(key)


def label(key: str) -> str:
    s = SPORTS.get(key)
    return s.label if s else key.upper()


def public_meta() -> list[dict]:
    """JSON-serializable metadata for a future /api/sports endpoint, so the
    frontend can build tabs/tiles/colors from the registry instead of
    hardcoding SPORT_LABEL / TIER_COLOR / the menu items."""
    return [{"key": s.key, "label": s.label, "emoji": s.emoji, "color": s.color,
             "kind": s.kind, "team": s.is_team, "has_props": s.has_props,
             "blurb": s.blurb, "source": s.source} for s in SPORTS.values()]
