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

# Tennis calibration: the raw Elo/surface model is overconfident on favorites
# (picks priced like 63% favorites were winning ~56%). Shrinking the probability
# toward 0.5 corrects that. It NEVER changes which player is favored, so the
# prediction win/loss record is unchanged — it only right-sizes confidence, which
# tightens edge/wager selection. Raise toward 1.0 as live calibration improves.
_TENNIS_CAL = 0.85


def _calibrate(p):
    try:
        return 0.5 + (float(p) - 0.5) * _TENNIS_CAL
    except (TypeError, ValueError):
        return p

_ATP_TOUR = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{y}.csv"
_ATP_CHAL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_qual_chall_{y}.csv"
_ATP_FUT = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_futures_{y}.csv"
_WTA_TOUR = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{y}.csv"
_WTA_CHAL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_qual_chall_{y}.csv"
_WTA_ITF = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_qual_itf_{y}.csv"

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
    """Map an ATP/WTA ranking to an Elo-ish rating for fallback use.

    Logarithmic so it keeps DISCRIMINATING all the way down the list, not just the
    top 200. The old exp(-rank/50) map flattened everyone past ~#300 to ~1500,
    which made every ITF / low-tier matchup a 50/50 coin flip. This spans the full
    range (#1≈2150 down to #1600≈1030), so two low-ranked players get a real,
    honest edge between them — which is what makes ITF predictable off the live
    ranking feed with no trained-ratings file needed. Players with trained history
    still use that (this only affects otherwise-unknown players)."""
    r = max(1, int(rank))
    return 2150.0 - 350.0 * math.log10(r)   # #1≈2150, #100≈1450, #300≈1283, #1000≈1100


class PredictionEngine:
    def __init__(self):
        self.model = TennisElo()
        self._by_key = {}          # name_key -> rating from history (Elo)
        self._rank_key = {}        # name_key -> rating from ranking (fallback)

    # ---- training --------------------------------------------------------

    def _ingest_url(self, url):
        import pandas as pd   # local import: only loaded during training/build, never at runtime
        try:
            # Sackmann CSVs are Latin-1 (accented player names). Reading them as the
            # default UTF-8 throws a decode error on the first accented name in EVERY
            # file, which used to be swallowed silently and produced an empty file.
            df = pd.read_csv(url, usecols=lambda c: c in _USECOLS,
                             encoding="latin-1", on_bad_lines="skip")
        except Exception as e:
            fn = url.rsplit("/", 1)[-1]
            print(f"[build] skip {fn}: {type(e).__name__}: {e}")
            return 0
        df = df.dropna(subset=["surface", "winner_name", "loser_name"])
        n = 0
        for row in df.itertuples(index=False):
            self.model.update(row.winner_name, row.loser_name, row.surface)
            n += 1
        del df
        return n

    def train_from_sackmann(self, years, include_challengers=True, include_futures=False):
        total = 0
        for y in years:
            urls = [_ATP_TOUR.format(y=y), _WTA_TOUR.format(y=y)]
            if include_challengers:
                urls += [_ATP_CHAL.format(y=y), _WTA_CHAL.format(y=y)]
            if include_futures:                      # ITF futures — men's + women's ITF
                urls += [_ATP_FUT.format(y=y), _WTA_ITF.format(y=y)]
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

    def predict_feed(self, name_a, name_b, surface=None):
        """
        Returns (prob_a, confidence) where confidence is:
          'high'   - both from match history
          'medium' - at least one from ranking fallback
          'low'    - a player couldn't be rated at all (prob defaults to 0.5)

        When `surface` is supplied and both players are history-rated, the base
        probability uses the surface-aware Elo (overall blended with how each
        player performs ON THAT SURFACE) instead of overall-only.
        """
        ra, sa = self._rating(name_a)
        rb, sb = self._rating(name_b)
        if ra is None or rb is None:
            return 0.5, "low"
        both_history = (sa == "history" and sb == "history")
        if surface and both_history:
            try:
                p = self.model.win_probability(name_a, name_b, surface, surface_weight=0.5)
                if p is not None:
                    return _calibrate(p), "high"
            except Exception:
                pass
        prob = expected_score(ra, rb)
        conf = "high" if both_history else "medium"
        return _calibrate(prob), conf

    def predict_feed_ctx(self, name_a, name_b, ctx=None, surface=None):
        """
        Like predict_feed, but applies small, data-backed adjustments when
        context is supplied. ctx (all optional):
          form_a / form_b      : recent win rate over last ~10 (0..1)
          fatigue_a / fatigue_b: recent load score (higher = more tired), 0..1
          h2h_a / h2h_b        : prior meetings won by each player (ints)
        Adjustments are deliberately conservative so the proven Elo base stays
        dominant; these refine the edge, they don't override it. `surface` is
        passed through so the base is surface-aware.
        """
        base, conf = self.predict_feed(name_a, name_b, surface=surface)
        if conf == "low" or not ctx:
            return base, conf
        # Work in Elo-point space: convert nudges to a rating delta, then re-expand.
        import math
        delta = 0.0  # positive favors A

        # Recent form: each 10% of win-rate edge -> up to ~25 Elo pts (capped).
        fa, fb = ctx.get("form_a"), ctx.get("form_b")
        if fa is not None and fb is not None:
            delta += max(-40, min(40, (fa - fb) * 80))

        # Fatigue: a more tired player loses a little; scale ~ up to 20 Elo pts.
        ta, tb = ctx.get("fatigue_a"), ctx.get("fatigue_b")
        if ta is not None and tb is not None:
            delta += max(-25, min(25, (tb - ta) * 50))  # if B more tired, favor A

        # Head-to-head: small Bayesian nudge from prior meetings (caps quickly).
        ha, hb = ctx.get("h2h_a"), ctx.get("h2h_b")
        if ha is not None and hb is not None and (ha + hb) > 0:
            edge = (ha - hb) / (ha + hb)          # -1..1
            weight = min(1.0, (ha + hb) / 5.0)     # more meetings -> more weight
            delta += edge * weight * 30

        # Convert base prob back to an implied rating gap, apply delta, re-expand.
        base = min(0.999, max(0.001, base))
        implied_gap = -400 * math.log10(1 / base - 1)   # inverse of expected_score
        adj_prob = 1.0 / (1.0 + 10 ** (-(implied_gap + delta) / 400))
        # never let adjustments flip more than ~12 percentage points
        adj_prob = max(base - 0.12, min(base + 0.12, adj_prob))
        return adj_prob, conf

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
