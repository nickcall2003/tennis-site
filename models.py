"""
elo.py
------
A tennis Elo rating engine with surface-specific ratings.

The idea is simple and is the same approach FiveThirtyEight used for tennis:

  1. Every player starts at 1500.
  2. Before a match, each player has an expected win probability based on the
     gap between their rating and the opponent's.
  3. After the match, the winner gains points and the loser loses the same
     number. Beating a much stronger player gains you more than beating a
     weaker one.
  4. We keep a SEPARATE rating per surface (Hard / Clay / Grass) on top of an
     overall rating, because a clay specialist and a grass specialist are very
     different players.

The only non-obvious piece is the K-factor (how much a single match moves a
rating). New players should move fast; established players with hundreds of
matches should be stable. So K shrinks as a player plays more matches:

        K = 250 / (matches_played + 5) ** 0.4

That formula is a well-known, battle-tested choice for tennis.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


# Surfaces we model. Anything else (e.g. "Carpet") falls back to overall-only.
SURFACES = ("Hard", "Clay", "Grass")

INITIAL_RATING = 1500.0


def _k_factor(matches_played: int) -> float:
    """How much a single result moves a rating. Shrinks as experience grows."""
    return 250.0 / (matches_played + 5) ** 0.4


def expected_score(rating_a: float, rating_b: float) -> float:
    """
    Probability that player A beats player B, given their two ratings.

    This is the standard Elo logistic curve. A 400-point gap means the
    favorite wins about 91% of the time; a 0-point gap is a coin flip.
    """
    return 1.0 / (1.0 + 10.0 ** ((rating_b - rating_a) / 400.0))


@dataclass
class TennisElo:
    """
    Holds every player's overall rating plus one rating per surface, and
    updates them as you feed in matches in chronological order.
    """

    # player_name -> overall rating
    overall: dict[str, float] = field(default_factory=lambda: defaultdict(lambda: INITIAL_RATING))
    # surface -> {player_name -> rating on that surface}
    surface: dict[str, dict[str, float]] = field(
        default_factory=lambda: {s: defaultdict(lambda: INITIAL_RATING) for s in SURFACES}
    )
    # how many matches each player has played (drives the K-factor)
    matches_overall: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    matches_surface: dict[str, dict[str, int]] = field(
        default_factory=lambda: {s: defaultdict(int) for s in SURFACES}
    )

    # ---- reading ratings -------------------------------------------------

    def get_overall(self, player: str) -> float:
        return self.overall[player]

    def get_surface(self, player: str, surface: str) -> float:
        if surface not in self.surface:
            # Unknown surface -> just use the overall rating.
            return self.overall[player]
        return self.surface[surface][player]

    # ---- prediction ------------------------------------------------------

    def win_probability(
        self,
        player_a: str,
        player_b: str,
        surface: str,
        surface_weight: float = 0.5,
    ) -> float:
        """
        Model probability that player_a beats player_b on a given surface.

        We blend two opinions:
          - the surface-specific Elo (how good are they ON CLAY, etc.)
          - the overall Elo (how good are they in general)

        surface_weight controls the mix. 0.5 = half and half. Bumping it
        toward 0.7 trusts surface form more; toward 0.3 trusts overall more.
        This is the single biggest knob to tune later against real results.
        """
        overall_p = expected_score(self.get_overall(player_a), self.get_overall(player_b))
        surface_p = expected_score(
            self.get_surface(player_a, surface), self.get_surface(player_b, surface)
        )
        if surface not in self.surface:
            return overall_p
        return surface_weight * surface_p + (1.0 - surface_weight) * overall_p

    # ---- learning --------------------------------------------------------

    def update(self, winner: str, loser: str, surface: str) -> None:
        """
        Feed in one completed match. Updates both overall and surface ratings.
        Call this for every match in chronological (oldest-first) order.
        """
        self._update_pool(self.overall, self.matches_overall, winner, loser)
        if surface in self.surface:
            self._update_pool(
                self.surface[surface], self.matches_surface[surface], winner, loser
            )

    @staticmethod
    def _update_pool(
        ratings: dict[str, float],
        counts: dict[str, int],
        winner: str,
        loser: str,
    ) -> None:
        r_win = ratings[winner]
        r_lose = ratings[loser]

        # Expected result for the winner (1.0 means certain win).
        exp_win = expected_score(r_win, r_lose)

        k_win = _k_factor(counts[winner])
        k_lose = _k_factor(counts[loser])

        # Winner's actual score is 1, loser's is 0. The surprise (1 - exp_win)
        # is how much the ratings move.
        ratings[winner] = r_win + k_win * (1.0 - exp_win)
        ratings[loser] = r_lose + k_lose * (0.0 - (1.0 - exp_win))

        counts[winner] += 1
        counts[loser] += 1

    # ---- convenience -----------------------------------------------------

    def leaderboard(self, surface: str | None = None, top: int = 15) -> list[tuple[str, float, int]]:
        """Return (player, rating, matches) sorted best-first."""
        if surface is None:
            ratings, counts = self.overall, self.matches_overall
        else:
            ratings, counts = self.surface[surface], self.matches_surface[surface]
        rows = [(p, r, counts[p]) for p, r in ratings.items()]
        rows.sort(key=lambda x: x[1], reverse=True)
        return rows[:top]
