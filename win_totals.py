"""
win_totals.py — projected season wins vs the sportsbook line.

The feature: a book posts Memphis at 8.5 regular-season wins. We walk their
schedule game by game, attach a model win probability to each, and get a
projected win total. If our projection is meaningfully clear of the line, that's
an over/under edge.

TWO WAYS TO PROJECT, AND WHY THE SOFT ONE IS RIGHT
  * Hard count: mark each game a projected W if p>=0.5, else L, and count the
    Ws. Simple, but it throws away all the information in the probability — a
    schedule of twelve 51% games "projects" 12-0, which is nonsense.
  * Expected wins: sum the win probabilities. Twelve 51% games = 6.12 expected
    wins, which is the honest number. We lead with this.
  The PROJECTED RECORD is now the rounded expected wins, so the record shown
  next to the projection agrees with it (a 42.0-win projection reads 42-40, not
  84-0). The per-game hard W/L marks remain in the walk-through for the readout.

This module is pure math over an interface you supply — a list of games each
with a win probability. It does NOT fetch schedules or compute Elo itself; the
caller passes those in. That keeps it testable and lets the same engine sit on
top of whatever schedule/rating source each sport uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Game:
    opponent: str
    win_prob: float                 # model P(this team wins), 0..1
    home: bool | None = None
    played: bool | None = None      # True=already final, result known
    won: bool | None = None         # if played, did they win
    date: str | None = None


@dataclass
class WinTotalProjection:
    team: str
    line: float | None              # sportsbook posted win total
    expected_wins: float            # sum of win probabilities (the honest number)
    projected_record: str           # rounded expected wins as "W-L"
    games_total: int
    games_played: int
    actual_wins: int                # wins already banked
    remaining_expected: float       # expected wins over unplayed games
    lean: str | None                # "over", "under", or None if no edge/line
    edge_wins: float | None         # expected wins minus the line
    confidence: str                 # how far clear of the line we are
    games: list = field(default_factory=list)


def project(team: str, games: list, line: float | None = None,
            edge_threshold: float = 0.75):
    """Build a win-total projection from a schedule of Games.

    edge_threshold: how many wins clear of the line we need before calling a
    lean. Below this, the projection and the line agree closely enough that
    there's no honest edge to claim. Default 0.75 wins — under a full game of
    separation is noise given model error."""
    played = [g for g in games if g.played]
    upcoming = [g for g in games if not g.played]

    actual_wins = sum(1 for g in played if g.won)
    # expected wins = banked wins + sum of remaining win probabilities
    remaining_expected = sum(_clip(g.win_prob) for g in upcoming)
    expected_wins = actual_wins + remaining_expected

    # Projected record reflects the EXPECTED wins (the honest number), NOT a hard
    # p>=0.5 count. The hard count marked every coin flip and slight favorite as a
    # guaranteed win, producing nonsense like 84-0 for a slate of 50/50 games and
    # a record that disagreed with the expected-wins headline beside it. Rounding
    # expected wins keeps the two consistent.
    total = len(games)
    proj_w = max(0, min(total, int(round(expected_wins))))
    proj_l = total - proj_w

    # Reconcile the WALK-THROUGH with the record: mark exactly (proj_w - banked)
    # upcoming games as projected wins — the highest-probability ones — so the
    # count of W's in the game-by-game readout equals the projected record, which
    # in turn is the rounded projected-wins headline. No more "17 green W's next
    # to a 14-3 record next to 13.5 wins."
    upcoming_idx = [i for i, g in enumerate(games) if not g.played]
    upcoming_idx.sort(key=lambda i: _clip(games[i].win_prob), reverse=True)
    n_upcoming_wins = max(0, min(len(upcoming_idx), proj_w - actual_wins))
    win_idx = set(upcoming_idx[:n_upcoming_wins])

    lean = None
    edge = None
    confidence = "n/a"
    if line is not None:
        edge = round(expected_wins - line, 2)
        if abs(edge) >= edge_threshold:
            lean = "over" if edge > 0 else "under"
        # separation in wins -> a rough confidence label
        a = abs(edge)
        confidence = ("strong" if a >= 2.0 else
                      "solid" if a >= 1.25 else
                      "slim" if a >= edge_threshold else
                      "no edge")

    return WinTotalProjection(
        team=team,
        line=line,
        expected_wins=round(expected_wins, 2),
        projected_record=f"{proj_w}-{proj_l}",
        games_total=total,
        games_played=len(played),
        actual_wins=actual_wins,
        remaining_expected=round(remaining_expected, 2),
        lean=lean,
        edge_wins=edge,
        confidence=confidence,
        games=[_game_view(g, projected_win=(i in win_idx)) for i, g in enumerate(games)],
    )


def _game_view(g: Game, projected_win=False):
    p = _clip(g.win_prob)
    if g.played:
        mark = "W" if g.won else "L"
        proj = None
    else:
        mark = None
        proj = "W" if projected_win else "L"
    return {
        "opponent": g.opponent,
        "home": g.home,
        "date": g.date,
        "win_prob": round(p * 100),
        "played": bool(g.played),
        "result": mark,               # actual, if played
        "projected": proj,            # our lean, if not
    }


def _clip(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, p))


def to_dict(proj: WinTotalProjection):
    return {
        "team": proj.team,
        "line": proj.line,
        "expected_wins": proj.expected_wins,
        "projected_record": proj.projected_record,
        "games_total": proj.games_total,
        "games_played": proj.games_played,
        "actual_wins": proj.actual_wins,
        "remaining_expected": proj.remaining_expected,
        "lean": proj.lean,
        "edge_wins": proj.edge_wins,
        "confidence": proj.confidence,
        "games": proj.games,
    }
