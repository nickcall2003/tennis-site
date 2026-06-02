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
        import pandas as pd   # local import: only loaded during training/build, never at runtime
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

    # ---- precomputed ratings (memory-saver) ------------------------------
    # Train once offline, export to a small JSON, and have the live server LOAD
    # that JSON instead of pandas+CSVs. Cuts the biggest memory user entirely
    # while preserving every bit of the historical training.

    def export_ratings(self, path):
        import json
        data = {
            "overall": self.model.overall,
            "surface": {s: dict(v) for s, v in self.model.surface.items()},
            "matches_overall": self.model.matches_overall,
            "by_key": self._by_key,
        }
        with open(path, "w") as f:
            json.dump(data, f)
        return len(self.model.overall)

    def load_ratings(self, path):
        """Load precomputed ratings. Returns player count, or 0 if unavailable."""
        import json
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return 0
        self.model.overall = {k: float(v) for k, v in data.get("overall", {}).items()}
        for s, v in data.get("surface", {}).items():
            self.model.surface[s] = {k: float(val) for k, val in v.items()}
        self.model.matches_overall = data.get("matches_overall", {}) or {}
        self._by_key = {k: float(v) for k, v in data.get("by_key", {}).items()}
        return len(self.model.overall)

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

    def analysis_facts(self, name_a, name_b, surface="Unknown"):
        """
        Structured, data-backed facts for the analysis writeup. Every value
        here is derived from the rating model, so nothing is invented.
        Returns a dict (all keys optional / None when not derivable).
        """
        ra, sa = self._rating(name_a)
        rb, sb = self._rating(name_b)
        facts = {"rated_a": ra is not None, "rated_b": rb is not None}
        if ra is None or rb is None:
            return facts

        # overall edge
        overall_p = expected_score(ra, rb)
        facts["overall_prob_a"] = round(overall_p, 3)
        facts["rating_gap"] = round(abs(ra - rb))
        facts["edge_size"] = ("decisive" if abs(ra - rb) >= 150 else
                              "clear" if abs(ra - rb) >= 70 else
                              "slight" if abs(ra - rb) >= 25 else "negligible")

        # surface-specific comparison (only if we have history-based surface ratings)
        if surface in self.model.surface and sa == "history" and sb == "history":
            sra = self.model.get_surface(name_a, surface)
            srb = self.model.get_surface(name_b, surface)
            surf_p = expected_score(sra, srb)
            facts["surface_prob_a"] = round(surf_p, 3)
            # does the surface help or hurt the favorite vs their overall level?
            fav_overall = overall_p >= 0.5
            fav_surface = surf_p >= 0.5
            delta = surf_p - overall_p
            facts["surface_swing"] = round(delta, 3)
            if abs(delta) >= 0.04:
                better = name_a if delta > 0 else name_b
                facts["surface_note"] = (
                    f"{surface} suits {better} more than their overall level suggests")
            facts["surface_aligned"] = (fav_overall == fav_surface)
        return facts

