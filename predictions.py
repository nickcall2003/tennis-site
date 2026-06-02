"""
predictions.py
--------------
The prediction model: surface-aware Elo trained on Jeff Sackmann's free history,
with two upgrades that cut "low-confidence" picks dramatically:

  1. We now train on TOUR-LEVEL **and** CHALLENGER history (Sackmann publishes
     both, free), so Challenger players are in the model instead of missing.
  2. Players still not found get a rating derived from their ATP/WTA RANKING
     (pulled from the API-Tennis feed you already pay for). Only players with
     neither history nor a ranking stay 50/50 / low-confidence.

Name matching is accent- and hyphen-insensitive, and works on both the feed's
abbreviated "M. Navone" and Sackmann's full "Mariano Navone".
"""

from __future__ import annotations

import math
import re
import unicodedata

import pandas as pd

from elo import TennisElo, expected_score

_ATP_TOUR = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{y}.csv"
_ATP_CHAL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_qual_chall_{y}.csv"
_WTA_TOUR = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{y}.csv"
_WTA_CHAL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_qual_chall_{y}.csv"

_USECOLS = ["tourney_date", "surface", "winner_name", "loser_name"]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _name_key(full_name: str):
    """last name + first initial, accent/hyphen-insensitive. 'M. Navone' & 'Mariano Navone' -> same."""
    if not full_name:
        return None
    name = _strip_accents(full_name.strip().lower()).replace("-", " ").replace(".", ". ")
    name = re.sub(r"\s+", " ", name).strip()
    m = re.match(r"^([a-z])\.\s+(.+)$", name)        # "m. navone"
    if m:
        initial, last = m.group(1), m.group(2).split()[-1]
    else:
        parts = name.split()
        if len(parts) < 2:
            return None
        initial, last = parts[0][0], parts[-1]
    return f"{last}|{initial}"


def _rank_to_rating(rank: int) -> float:
    """Map an ATP/WTA ranking to an Elo-ish rating for fallback use."""
    return 1500.0 + 700.0 * math.exp(-rank / 50.0)   # #1≈2186, #50≈1757, #200≈1513


class PredictionEngine:
    def __init__(self):
        self.model = TennisElo()
        self._by_key = {}          # name_key -> rating from history (Elo)
        self._rank_key = {}        # name_key -> rating from ranking (fallback)

    # ---- training --------------------------------------------------------

    def _ingest_url(self, url):
        try:
            df = pd.read_csv(url, usecols=lambda c: c in _USECOLS)
        except Exception:
            return 0
        df = df.dropna(subset=["surface", "winner_name", "loser_name"])
        n = 0
        for row in df.itertuples(index=False):
            self.model.update(row.winner_name, row.loser_name, row.surface)
            n += 1
        del df
        return n

    def train_from_sackmann(self, years, include_challengers=True):
        total = 0
        for y in years:
            urls = [_ATP_TOUR.format(y=y), _WTA_TOUR.format(y=y)]
            if include_challengers:
                urls += [_ATP_CHAL.format(y=y), _WTA_CHAL.format(y=y)]
            for url in urls:
                total += self._ingest_url(url)
        # index best rating per name-key
        for name, rating in self.model.overall.items():
            k = _name_key(name)
            if k and rating > self._by_key.get(k, 0):
                self._by_key[k] = rating
        return total

    def load_rankings(self, rankings: dict):
        """rankings: {player_name: rank_int} from the feed. Builds the fallback."""
        for name, rank in (rankings or {}).items():
            k = _name_key(name)
            if not k:
                continue
            rating = _rank_to_rating(rank)
            if rating > self._rank_key.get(k, 0):
                self._rank_key[k] = rating

    def train_from_csv(self, path):
        self._ingest_url(path)

    def preset_demo_ratings(self):
        demo = {"Carlos Alcaraz": 2120, "Casper Ruud": 1870, "Jannik Sinner": 2150,
                "Daniil Medvedev": 1980, "Iga Swiatek": 2100, "Aryna Sabalenka": 2060,
                "Jakub Mensik": 1750, "Dalibor Svrcina": 1680}
        for name, rating in demo.items():
            self.model.overall[name] = float(rating)
            for surf in ("Hard", "Clay", "Grass"):
                self.model.surface[surf][name] = float(rating)
            k = _name_key(name)
            if k:
                self._by_key[k] = float(rating)

    # ---- prediction ------------------------------------------------------

    def _rating(self, name):
        """(rating, source) where source is 'history' | 'ranking' | None."""
        k = _name_key(name)
        if k and k in self._by_key:
            return self._by_key[k], "history"
        if k and k in self._rank_key:
            return self._rank_key[k], "ranking"
        return None, None

    def predict_feed(self, name_a, name_b):
        """
        Returns (prob_a, confidence) where confidence is:
          'high'   - both from match history
          'medium' - at least one from ranking fallback
          'low'    - a player couldn't be rated at all (prob defaults to 0.5)
        """
        ra, sa = self._rating(name_a)
        rb, sb = self._rating(name_b)
        if ra is None or rb is None:
            return 0.5, "low"
        prob = expected_score(ra, rb)
        conf = "high" if (sa == "history" and sb == "history") else "medium"
        return prob, conf

    def predict(self, player_a, player_b, surface):
        return self.model.win_probability(player_a, player_b, surface, surface_weight=0.5)
