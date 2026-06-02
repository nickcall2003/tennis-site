"""
predictions.py
--------------
Wraps the Elo engine from the prototype. In production you call
train_from_csv() on Sackmann's data once a day; for the demo we preset a few
ratings so probabilities are sensible without any data download.
"""

from __future__ import annotations

import re

import pandas as pd

from elo import TennisElo, expected_score

_SACKMANN_ATP = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{y}.csv"
_SACKMANN_WTA = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{y}.csv"


def _name_key(full_name: str) -> str | None:
    """
    Turn a player name into a join key: last name + first initial.
    Works for both 'Mariano Navone' (Sackmann) and 'M. Navone' (the feed),
    so the two data sources line up without exact-string matching.
    """
    if not full_name:
        return None
    name = full_name.strip()
    m = re.match(r"^([A-Za-z])\.\s+(.+)$", name)        # "M. Navone"
    if m:
        initial, last = m.group(1), m.group(2)
    else:
        parts = name.split()
        if len(parts) < 2:
            return None
        initial, last = parts[0][0], parts[-1]
    return f"{last.lower().replace('-', ' ').strip()}|{initial.lower()}"


class PredictionEngine:
    def __init__(self) -> None:
        self.model = TennisElo()
        self._by_key: dict[str, float] = {}   # name_key -> best overall rating

    def train_from_csv(self, path: str) -> None:
        """Replay a match-history CSV (tourney_date, surface, winner_name, loser_name)."""
        df = pd.read_csv(path)
        self._ingest(df)

    def train_from_sackmann(self, years) -> None:
        """Download recent ATP+WTA results and train. Needs internet (runs on your host)."""
        frames = []
        for y in years:
            for url in (_SACKMANN_ATP.format(y=y), _SACKMANN_WTA.format(y=y)):
                try:
                    frames.append(pd.read_csv(url))
                except Exception:
                    pass
        if frames:
            self._ingest(pd.concat(frames, ignore_index=True))

    def _ingest(self, df: pd.DataFrame) -> None:
        df = df.dropna(subset=["surface", "winner_name", "loser_name"])
        for row in df.itertuples(index=False):
            self.model.update(row.winner_name, row.loser_name, row.surface)
        # Build the name-key index (best rating wins on collisions).
        for name, rating in self.model.overall.items():
            k = _name_key(name)
            if k and rating > self._by_key.get(k, 0):
                self._by_key[k] = rating

    def preset_demo_ratings(self) -> None:
        """Approximate ratings for the demo players so the MVP shows real spread."""
        demo = {
            "Carlos Alcaraz": 2120, "Casper Ruud": 1870, "Jannik Sinner": 2150,
            "Daniil Medvedev": 1980, "Iga Swiatek": 2100, "Aryna Sabalenka": 2060,
            "Jakub Mensik": 1750, "Dalibor Svrcina": 1680,
            "Local Qualifier": 1500, "Wildcard Entry": 1480,
        }
        for name, rating in demo.items():
            self.model.overall[name] = float(rating)
            for surf in ("Hard", "Clay", "Grass"):
                self.model.surface[surf][name] = float(rating)

    def predict(self, player_a: str, player_b: str, surface: str) -> float:
        """Model probability that player_a beats player_b."""
        return self.model.win_probability(player_a, player_b, surface, surface_weight=0.5)

    def predict_feed(self, name_a: str, name_b: str) -> tuple[float, bool]:
        """
        Probability for feed-supplied (abbreviated) names. Returns
        (prob_a, confident). `confident` is False when we couldn't match one
        of the players to our ratings, so the UI can flag it.
        """
        ra = self._by_key.get(_name_key(name_a) or "")
        rb = self._by_key.get(_name_key(name_b) or "")
        if ra is None or rb is None:
            return 0.5, False
        return expected_score(ra, rb), True
