"""
apitennis.py
------------
Real data-feed adapter for API-Tennis (https://api-tennis.com).

Covers ATP / WTA / Challenger (ITF/doubles filtered out). Your key is read
from TENNIS_API_KEY and only ever used server-side.

Endpoints used (from API-Tennis docs):
  get_fixtures   -> a day's matches (with event_time, tournament, player keys)
  get_livescore  -> all currently-live matches at once
  get_standings  -> ATP / WTA rankings  (used to rate otherwise-unknown players)
  get_H2H        -> head-to-head + each player's recent results
  (point-by-point arrives inside get_fixtures for a specific match key)

Usage protection: the live-score poll is the only call that runs continuously,
so it's throttled by TENNIS_LIVE_TTL (default 15s) and a per-day soft cap
(TENNIS_DAILY_MAX) pauses *only* live polling near the plan limit, leaving the
cheap, essential calls (schedule, H2H, rankings) working. Errors are logged and
exposed via .last_error so an account/billing failure is never invisible.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from base import LiveScore, MatchInfo, MatchStats, TennisProvider

BASE_URL = "https://api.api-tennis.com/tennis/"

# ITF/futures ingestion is ON. Match volume is handled cheaply (the odds warmer
# prices the whole day's slate in ONE api-tennis call, so ITF adds no odds cost).
# A kill-switch remains for emergencies only — set INCLUDE_ITF=0 to disable — but
# no variable is needed to turn ITF ON; it's the default.
_INCLUDE_ITF = os.environ.get("INCLUDE_ITF", "1").strip().lower() not in ("0", "false", "no", "off")


def _fmt_tournament(name):
    """Append the host country to ITF labels ('M25 Skopje' -> 'M25 Skopje
    (North Macedonia)'). Best-effort; if the helper module is missing or the city
    is unknown, the original name is returned unchanged."""
    try:
        import tennis_cities
        return tennis_cities.format_tournament(name)
    except Exception:
        return name

_TIER_MAP = {
    "Atp Singles": "ATP",
    "Wta Singles": "WTA",
    "Challenger Men Singles": "CHALLENGER",
    "Challenger Women Singles": "CHALLENGER",
}


_CLAY_KW = ("roland garros", "french open", "monte-carlo", "monte carlo", "madrid open",
            "mutua madrid", "rome", "internazionali", "italian open", "barcelona",
            "hamburg", "munich", "bavarian", "estoril", "geneva", "lyon", "gstaad",
            "kitzbuhel", "kitzbuehel", "umag", "bastad", "marrakech", "rabat",
            "bogota", "houston", "charleston", "parma", "belgrade", "cordoba",
            "rio de janeiro", "rio open", "buenos aires", "santiago", "sao paulo",
            "iasi", "palermo", "warsaw", "prague", "strasbourg", "rouen",
            "saint-malo", "saint malo", "tunis", "porsche")
_GRASS_KW = ("wimbledon", "halle", "queen", "eastbourne", "mallorca",
             "hertogenbosch", "bois-le-duc", "'s-hertogenbosch", "newport",
             "bad homburg", "nottingham", "birmingham", "ilkley", "surbiton")
# Same city, different surface by tour: resolve with the tier we already classified.
_SURFACE_OVERRIDE = (
    ("stuttgart", "ATP", "Grass"),   # Boss Open is grass (men)
    ("stuttgart", "WTA", "Clay"),    # Porsche Tennis GP is indoor clay (women)
    ("berlin", "WTA", "Grass"),      # WTA Berlin is grass
)


def _infer_surface(name, tier=None, when=None):
    """Best-effort surface for a tournament. api-tennis carries no surface field,
    so map by tournament name (tier-aware for the few cities that differ by tour),
    then fall back to the clay/grass calendar windows, else Hard (the modal
    surface). Feeds both the Surface-tab highlight and surface-aware predictions."""
    n = (name or "").lower()
    for kw, tr, surf in _SURFACE_OVERRIDE:
        if kw in n and tr == tier:
            return surf
    if any(k in n for k in _GRASS_KW):
        return "Grass"
    if any(k in n for k in _CLAY_KW):
        return "Clay"
    # Season fallback is CLAY-ONLY: every grass event is named in _GRASS_KW, so we
    # never guess grass from the calendar (that wrongly painted July hard/clay
    # events grass during the grass weeks). Unknowns default to Hard, the modal.
    try:
        mo, dy = when.month, when.day
        if mo in (4, 5) or (mo == 6 and dy <= 8):              # European clay swing
            return "Clay"
    except Exception:
        pass
    return "Hard"


def _classify_tier(fix):
    """
    Decide the tier from an API fixture, robustly. The feed's exact
    'event_type_type' strings vary, so we classify by keyword and, crucially,
    by gender. The key fix: women's events (incl. WTA 125 "challenger"-style
    events like Birmingham/Makarska/Foggia) must map to WTA, not CHALLENGER.
    Singles only; doubles excluded. ITF/futures excluded as below tour level
    UNLESS the INCLUDE_ITF env flag is set — then they're tagged 'ITF' so they can
    be tracked as their own sub-category (never folded into the main tennis units).
    NOTE: ITF is very high-volume; enabling it materially increases api-tennis
    calls, so it stays off by default.
    """
    et = (fix.get("event_type_type") or "").strip().lower()
    name = (fix.get("tournament_name") or "").lower()
    hay = et + " " + name

    # exclude doubles explicitly (singles product only)
    if "doubles" in hay or "/" in (fix.get("event_first_player") or ""):
        return None

    # Exclude national-team competitions (Davis Cup, Billie Jean King Cup,
    # United Cup, Laver/ATP Cup). api-tennis tags these "Teams Men"/"Teams Women"
    # and lists them country-vs-country (e.g. "Armenia" vs "Albania") with no
    # player-level odds, so they are not bettable singles and must never enter
    # the slate — otherwise every tie shows up as a phantom match "awaiting
    # market". The word "men"/"women" in "Teams Men/Women" was wrongly tagging
    # them ATP/WTA below.
    if "teams" in hay or any(k in name for k in (
            "davis cup", "billie jean king", "united cup",
            "laver cup", "atp cup", "fed cup")):
        return None

    is_women = ("women" in hay or "wta" in hay or "ladies" in hay
                or "girls" in hay)
    is_men = ("men" in hay or "atp" in hay or "boys" in hay) and not is_women
    is_chall = "challenger" in hay
    is_itf = "itf" in hay or "futures" in hay or "m15" in hay or "m25" in hay \
             or "w15" in hay or "w25" in hay or "w35" in hay or "w50" in hay \
             or "w75" in hay or "w100" in hay

    # Women first: any women's tour event is WTA (including 125/"challenger" tier).
    # Genuine ITF women's futures (w15..w100) stay excluded.
    if is_women:
        if is_itf:
            return "ITF" if _INCLUDE_ITF else None
        return "WTA"
    # Men's challenger
    if is_chall and not is_itf:
        return "CHALLENGER"
    # Men's tour
    if is_men:
        if is_itf:
            return "ITF" if _INCLUDE_ITF else None
        return "ATP"
    # ATP/WTA explicit but gender not otherwise flagged
    if "atp" in hay:
        return "CHALLENGER" if is_chall else "ATP"
    # fall back to the original exact map if present; otherwise exclude.
    return _TIER_MAP.get(fix.get("event_type_type"))


# Live poll cadence + per-day usage guard (all env-tunable).
_LIVE_TTL = float(os.environ.get("TENNIS_LIVE_TTL", "15"))       # was 8; halves live-poll cost
_FIXTURE_TTL = float(os.environ.get("TENNIS_FIXTURE_TTL", "20"))  # detail-pull cache
_ODDS_TTL = float(os.environ.get("TENNIS_ODDS_TTL", "120"))      # whole-day odds cache
_DAILY_MAX = int(os.environ.get("TENNIS_DAILY_MAX", "7500"))      # soft cap under an 8k/day plan


def _best_dec(book_map):
    """Highest decimal price across bookmakers (the best price for the bettor)."""
    best = None
    for v in (book_map or {}).values():
        try:
            d = float(v)
        except (TypeError, ValueError):
            continue
        if d > 1.0 and (best is None or d > best):
            best = d
    return best


def _server(flag) -> str:
    return "a" if flag == "First Player" else "b" if flag == "Second Player" else "a"


def _winner(flag) -> str | None:
    return "a" if flag == "First Player" else "b" if flag == "Second Player" else None


def _sets(scores):
    a, b = [], []
    for s in scores or []:
        try:
            a.append(int(s.get("score_first", 0)))
            b.append(int(s.get("score_second", 0)))
        except (ValueError, TypeError):
            pass
    return a, b


def _game(result):
    if not result or str(result).strip() in ("-", ""):
        return "0", "0"
    parts = [p.strip() for p in str(result).split("-")]
    return (parts[0], parts[1]) if len(parts) == 2 else ("0", "0")


def _status(fix):
    st = (fix.get("event_status") or "").strip()
    if st == "Finished" or fix.get("event_winner"):
        return "finished"
    if fix.get("event_live") == "1" or st.startswith("Set"):
        return "live"
    return "scheduled"


class APITennisProvider(TennisProvider):
    name = "apitennis"

    def __init__(self, api_key=None, timezone=None):
        self.api_key = api_key or os.environ.get("TENNIS_API_KEY")
        if not self.api_key:
            raise RuntimeError("Set TENNIS_API_KEY to use the live API-Tennis feed.")
        self.timezone = timezone or os.environ.get("TENNIS_TZ", "America/Chicago")
        self._fixtures = {}            # event_key -> raw fixture (latest seen)
        self._live_cache = {}
        self._live_fetched_at = 0.0
        self._detail_cache = {}        # event_key -> (ts, raw fixture with pbp)
        self._pavg_cache = {}          # player_key -> (ts, season serve/return averages)
        self._odds_cache = {}          # day/match key -> (ts, parsed odds dict)
        self._req_count = 0            # API requests made today (usage meter)
        self._req_day = None
        self.last_error = None         # last API error envelope, surfaced in the diag

    # ---- HTTP ------------------------------------------------------------

    def _call(self, method, **params):
        import httpx
        import datetime as _dt
        # Per-day usage meter + soft guard. The live poll is the only call that
        # runs continuously, so near the daily limit we pause ONLY get_livescore
        # and let the cheap, essential calls (schedule/H2H/rankings) through.
        today = _dt.date.today()
        if self._req_day != today:
            self._req_day, self._req_count = today, 0
        if method == "get_livescore" and self._req_count >= _DAILY_MAX:
            self.last_error = (f"daily cap {_DAILY_MAX} reached; live polling paused "
                               "until tomorrow")
            return []
        params = {"method": method, "APIkey": self.api_key, "timezone": self.timezone, **params}
        self._req_count += 1
        r = httpx.get(BASE_URL, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
        if not data or data.get("success") != 1:
            # api-tennis returns HTTP 200 even on errors (e.g. cod 1006 "Please
            # make the payment for your account!"). Surface the message instead of
            # silently returning [] so a billing/auth failure is visible.
            msg = None
            if isinstance(data, dict):
                res = data.get("result")
                if isinstance(res, list) and res and isinstance(res[0], dict):
                    msg = res[0].get("msg") or res[0].get("error")
                msg = msg or data.get("error") or data.get("message")
            self.last_error = f"{method}: {msg or data}"
            print(f"[apitennis] no data ({self.last_error})")
            return []
        self.last_error = None
        return data.get("result", []) or []

    # ---- schedule --------------------------------------------------------

    def get_schedule(self, day: datetime):
        d = day.strftime("%Y-%m-%d")
        rows = self._call("get_fixtures", date_start=d, date_stop=d)
        out = []
        for fix in rows:
            tier = _classify_tier(fix)
            if tier is None:
                continue
            key = str(fix.get("event_key"))
            self._fixtures[key] = fix
            try:
                when = datetime.strptime(
                    f"{fix.get('event_date')} {fix.get('event_time','00:00')}", "%Y-%m-%d %H:%M")
            except ValueError:
                when = day
            out.append(MatchInfo(
                provider_match_id=key, tier=tier,
                tournament=_fmt_tournament(fix.get("tournament_name", "Tennis")),
                surface=_infer_surface(fix.get("tournament_name", ""), tier, when),
                player_a=fix.get("event_first_player", "Player A"),
                player_b=fix.get("event_second_player", "Player B"),
                scheduled=when, best_of=3, status=_status(fix),
            ))
        return out

    def raw_fixture_probe(self, match_id):
        """Report what data a match's FULL fixture carries (statistics /
        point-by-point) — fetched per-match via raw_fixture so it includes detail
        the bulk day cache strips out. Best run on a live or just-finished match."""
        fix = self.raw_fixture(match_id) or {}
        stats = fix.get("statistics") or fix.get("statistic")
        pbp = fix.get("pointbypoint") or fix.get("point_by_point")
        scores = fix.get("scores") or fix.get("score")
        return {
            "found": bool(fix),
            "top_level_keys": sorted(fix.keys()),
            "has_statistics": bool(stats),
            "statistics_sample": (stats[:12] if isinstance(stats, list) else stats),
            "has_pointbypoint": bool(pbp),
            "has_scores": bool(scores),
            "status": fix.get("event_status") or fix.get("event_live"),
            "players": [fix.get("event_first_player"), fix.get("event_second_player")],
        }

    def fixture_meta(self, provider_match_id):
        """Extra fields we keep but the neutral MatchInfo doesn't carry."""
        fix = self._fixtures.get(str(provider_match_id), {})
        return {
            "event_time": fix.get("event_time"),
            "tournament_key": str(fix.get("tournament_key") or ""),
            "round": fix.get("tournament_round") or "",
            "player_a_key": str(fix.get("event_first_player_key") or ""),
            "player_b_key": str(fix.get("event_second_player_key") or ""),
            "player_a_logo": fix.get("event_first_player_logo"),
            "player_b_logo": fix.get("event_second_player_logo"),
        }

    # ---- live ------------------------------------------------------------

    def _refresh_live(self):
        if time.time() - self._live_fetched_at < _LIVE_TTL:
            return
        self._live_fetched_at = time.time()
        try:
            rows = self._call("get_livescore")
        except Exception:
            return
        cache = {}
        for fix in rows:
            if _classify_tier(fix) is None:
                continue
            key = str(fix.get("event_key"))
            self._fixtures[key] = fix
            sa, sb = _sets(fix.get("scores"))
            ga, gb = _game(fix.get("event_game_result"))
            cache[key] = LiveScore(sets_a=sa, sets_b=sb, game_a=ga, game_b=gb,
                                   server=_server(fix.get("event_serve")),
                                   status="live", winner=None)
        self._live_cache = cache

    def final_results(self, day):
        """{event_key: (status, winner)} for a day's fixtures. Used to reconcile
        matches the live feed dropped before they were marked finished (a finished
        match leaves get_livescore, so the per-match poll can't see its winner)."""
        d = day.strftime("%Y-%m-%d") if hasattr(day, "strftime") else str(day)
        try:
            rows = self._call("get_fixtures", date_start=d, date_stop=d)
        except Exception:
            return {}
        out = {}
        for fix in rows or []:
            k = str(fix.get("event_key"))
            if not k:
                continue
            st = _status(fix)
            out[k] = (st, _winner(fix.get("event_winner")) if st == "finished" else None)
        return out

    def get_live_score(self, provider_match_id):
        self._refresh_live()
        key = str(provider_match_id)
        if key in self._live_cache:
            return self._live_cache[key]
        fix = self._fixtures.get(key)
        if not fix:
            return LiveScore(status="scheduled")
        sa, sb = _sets(fix.get("scores"))
        st = _status(fix)
        return LiveScore(sets_a=sa, sets_b=sb, game_a="", game_b="",
                         server=_server(fix.get("event_serve")), status=st,
                         winner=_winner(fix.get("event_winner")) if st == "finished" else None)

    def get_match_stats(self, provider_match_id):
        # Kept for the narrow interface; the rich detail path uses raw_fixture().
        return MatchStats()

    # ---- detail (point-by-point, for the match page) ---------------------

    def match_statistics(self, match_id):
        """Parsed serve/return detail for ONE match, ready for the detail sheet.
        Returns per-player match-level stats (aces, 1st/2nd serve, break points,
        return points, etc.) plus live status, score, and who's serving."""
        fix = self.raw_fixture(match_id) or {}
        p1key = str(fix.get("first_player_key") or "")
        p2key = str(fix.get("second_player_key") or "")
        out = {
            "match_key": str(match_id),
            "live": str(fix.get("event_live") or "") == "1",
            "status": fix.get("event_status"),
            "score": fix.get("event_final_result") or fix.get("event_game_result"),
            "serving": fix.get("event_serve"),
            "tournament": _fmt_tournament(fix.get("tournament_name")),
            "round": fix.get("tournament_round"),
            "p1": {"name": fix.get("event_first_player"), "key": p1key, "stats": {}},
            "p2": {"name": fix.get("event_second_player"), "key": p2key, "stats": {}},
        }
        for s in (fix.get("statistics") or []):
            if not isinstance(s, dict) or s.get("stat_period") != "match":
                continue                         # match totals only (skip per-set)
            pk = str(s.get("player_key") or "")
            slot = "p1" if pk == p1key else ("p2" if pk == p2key else None)
            if not slot:
                continue
            out[slot]["stats"][s.get("stat_name")] = {
                "type": s.get("stat_type"),
                "value": s.get("stat_value"),
                "won": s.get("stat_won"),
                "total": s.get("stat_total"),
            }
        out["has_stats"] = bool(out["p1"]["stats"] or out["p2"]["stats"])
        return out

    def player_season_keys(self, player_key, since_days=300, limit=30):
        """Most-recent-first finished match keys for a player over the season,
        via a single get_fixtures(player_key) call. Used to widen serve/return
        averages beyond the handful the H2H call returns."""
        import datetime as _dt
        pk = str(player_key or "")
        if not pk:
            return []
        today = _dt.date.today()
        start = today - _dt.timedelta(days=since_days)
        try:
            rows = self._call("get_fixtures", date_start=start.strftime("%Y-%m-%d"),
                              date_stop=today.strftime("%Y-%m-%d"), player_key=pk)
        except Exception:
            return []
        dated = []
        for fx in (rows or []):
            if not isinstance(fx, dict) or str(fx.get("event_live") or "") == "1":
                continue
            k = str(fx.get("event_key") or "")
            if k:
                surf = _infer_surface(fx.get("tournament_name"), when=fx.get("event_date"))
                dated.append((fx.get("event_date") or "", k, surf))
        dated.sort(reverse=True)
        out, seen = [], set()
        for d, k, surf in dated:
            if k in seen:
                continue
            seen.add(k); out.append({"key": k, "surface": surf, "date": d})
            if len(out) >= limit:
                break
        return out

    def player_serve_averages(self, player_key, match_keys, surface=None, max_matches=15, min_surface=5):
        """Aggregate a player's serve/return stats into season averages. When a
        surface is given and the player has at least `min_surface` matches on it,
        average ONLY those (surface profiles differ a lot in tennis); otherwise
        fall back to their rolling recent matches across all surfaces. Pulls the
        season match list (falling back to supplied recent keys) and caches 24h."""
        if not player_key:
            return None
        ck = f"{player_key}|{surface or 'all'}"
        c = self._pavg_cache.get(ck)
        if c and time.time() - c[0] < 86400:
            return c[1]
        pk = str(player_key)
        season = self.player_season_keys(player_key)            # [{key,surface,date}...]
        surf_keys = [m["key"] for m in season if surface and m.get("surface") == surface]
        surface_filtered = bool(surface) and len(surf_keys) >= min_surface
        if surface_filtered:
            keys = surf_keys
        elif season:
            keys = [m["key"] for m in season]
        else:
            keys = list(match_keys or [])
        acc, counts, used = {}, {}, 0
        for mk in keys[:max_matches * 2]:
            if used >= max_matches:
                break
            try:
                fix = self.raw_fixture(mk)
            except Exception:
                continue
            got = False
            for s in ((fix or {}).get("statistics") or []):
                if not isinstance(s, dict) or s.get("stat_period") != "match":
                    continue
                if str(s.get("player_key")) != pk:
                    continue
                name = s.get("stat_name")
                w, t = s.get("stat_won"), s.get("stat_total")
                if w is not None and t:                       # ratio stat -> sum won/total
                    a = acc.setdefault(name, [0, 0])
                    a[0] += w; a[1] += t; got = True
                else:                                         # count stat (aces, DFs) -> average
                    val = s.get("stat_value")
                    try:
                        counts.setdefault(name, []).append(float(str(val).replace("%", "")))
                        got = True
                    except (TypeError, ValueError):
                        pass
            if got:
                used += 1
        if used == 0:
            self._pavg_cache[ck] = (time.time(), None)
            return None

        def rpct(name):
            a = acc.get(name)
            return round(a[0] / a[1] * 100) if a and a[1] else None

        def cavg(name):
            v = counts.get(name)
            return round(sum(v) / len(v), 1) if v else None
        sp, rp = acc.get("Service Points Won", [0, 0]), acc.get("Return Points Won", [0, 0])
        out = {
            "_matches": used,
            "_surface": surface if surface_filtered else None,
            "_surface_filtered": surface_filtered,
            "service_points_won": sp[0], "service_points_total": sp[1], "service_points_pct": rpct("Service Points Won"),
            "return_points_won": rp[0], "return_points_total": rp[1], "return_points_pct": rpct("Return Points Won"),
            "break_points_saved": acc.get("Break Points Saved", [0, 0])[0], "break_points_faced": acc.get("Break Points Saved", [0, 0])[1],
            "break_points_won": acc.get("Break Points Converted", [0, 0])[0], "break_points_chances": acc.get("Break Points Converted", [0, 0])[1],
            "service_games_won": 0, "service_games_total": 0, "service_games_pct": None, "games_won": 0,
            "total_points_won": rpct("Total Points Won"),
            "aces": cavg("Aces"), "double_faults": cavg("Double Faults"),
            "first_serve_pct": rpct("1st serve points won") if "1st serve points won" in acc else cavg("1st serve percentage"),
            "first_serve_won_pct": rpct("1st serve points won"),
            "second_serve_won_pct": rpct("2nd serve points won"),
        }
        self._pavg_cache[ck] = (time.time(), out)
        return out

    def raw_fixture(self, provider_match_id):
        """
        Full fixture for ONE match, including point-by-point. Cached briefly so
        a live detail page polling every few seconds doesn't hammer the feed.
        """
        key = str(provider_match_id)
        cached = self._detail_cache.get(key)
        if cached and time.time() - cached[0] < _FIXTURE_TTL:
            return cached[1]
        rows = []
        try:
            rows = self._call("get_fixtures", match_key=key)
        except Exception:
            pass
        fix = rows[0] if rows else self._fixtures.get(key, {})
        if fix:
            self._fixtures[key] = fix
            self._detail_cache[key] = (time.time(), fix)
        return fix

    # ---- rankings (fills the rating model for unknown players) -----------

    def get_rankings(self):
        """Return {normalized_name: rank_int} for ATP + WTA."""
        out = {}
        for ev in ("ATP", "WTA"):
            try:
                rows = self._call("get_standings", event_type=ev)
            except Exception:
                continue
            for r in rows or []:
                name = r.get("player")
                place = r.get("place") or r.get("rank")
                if not name or place in (None, ""):
                    continue
                try:
                    out[name] = int(str(place).strip())
                except ValueError:
                    pass
        return out

    # ---- odds (match-winner / Home-Away market) --------------------------

    def get_odds(self, day=None, match_key=None):
        """
        Match-winner (Home/Away) odds, INCLUDED in the api-tennis plan.

        Returns { str(event_key): {"a": best_dec, "b": best_dec,
                                   "first": first_player, "second": second_player} }
        where 'a' = first player (Home), 'b' = second player (Away), and best_dec
        is the highest decimal across books (best price for the bettor). Pass a
        `day` to pull the whole slate in ONE call (cached ~120s), or a single
        match via match_key. Returns {} on failure.

        api-tennis get_odds returns result as a DICT keyed by match_key:
          result[mk]["Home/Away"]["Home"|"Away"] = {bookmaker: "decimal", ...}
        """
        params = {}
        if match_key:
            params["match_key"] = str(match_key)
        elif day is not None:
            d = day.strftime("%Y-%m-%d")
            params["date_start"] = d
            params["date_stop"] = d
        ck = params.get("match_key") or params.get("date_start") or "all"
        hit = self._odds_cache.get(ck)
        if hit and time.time() - hit[0] < _ODDS_TTL:
            return hit[1]
        try:
            res = self._call("get_odds", **params)
        except Exception:
            return {}
        out = {}
        if isinstance(res, dict):
            for mk, markets in res.items():
                ha = (markets or {}).get("Home/Away") or {}
                ba = _best_dec(ha.get("Home"))
                bb = _best_dec(ha.get("Away"))
                if not (ba and bb):
                    continue
                fix = self._fixtures.get(str(mk)) or {}
                out[str(mk)] = {"a": ba, "b": bb,
                                "first": fix.get("event_first_player"),
                                "second": fix.get("event_second_player")}
        self._odds_cache[ck] = (time.time(), out)
        return out

    # ---- head to head ----------------------------------------------------

    def get_h2h(self, key_a, key_b):
        """Raw get_H2H result dict, or {} on failure."""
        if not key_a or not key_b:
            return {}
        try:
            res = self._call("get_H2H", first_player_key=key_a, second_player_key=key_b)
        except Exception:
            return {}
        # get_H2H returns a dict (not a list) under result; _call already unwrapped.
        return res if isinstance(res, dict) else {}

    def get_match_context(self, key_a, key_b, match_dt=None):
        """
        Derive form / fatigue / H2H from one get_H2H call (it returns both the
        head-to-head and each player's recent results). Returns a dict ready for
        PredictionEngine.predict_feed_ctx, or {} if unavailable.

          form_x    : win rate over the player's last ~10 results (0..1)
          fatigue_x : share of those recent matches played in the last 7 days
          h2h_x     : meetings won by each player
        """
        import datetime as _dt
        data = self.get_h2h(key_a, key_b)
        if not data:
            return {}
        ctx = {}

        # --- head to head ---
        h2h = data.get("H2H") or data.get("firstPlayer_VS_secondPlayer") or []
        if isinstance(h2h, list) and h2h:
            wa = wb = 0
            for m in h2h:
                w = (m.get("event_winner") or "").lower()
                # event_winner is "First Player" / "Second Player"
                if "first" in w:
                    wa += 1
                elif "second" in w:
                    wb += 1
            if wa or wb:
                ctx["h2h_a"], ctx["h2h_b"] = wa, wb

        # --- recent form + fatigue, per player ---
        def _player_stats(results_key):
            results = data.get(results_key) or []
            if not isinstance(results, list) or not results:
                return None, None
            recent = results[:10]
            wins = 0
            recent_count = 0
            now = match_dt or _dt.datetime.now()
            for m in recent:
                ew = (m.get("event_winner") or "").lower()
                # the player whose list this is appears as "First Player" in their own list
                if "first" in ew:
                    wins += 1
                # fatigue: did this match happen within 7 days before the match?
                ds = m.get("event_date") or ""
                try:
                    d = _dt.datetime.strptime(ds, "%Y-%m-%d")
                    if 0 <= (now - d).days <= 7:
                        recent_count += 1
                except (ValueError, TypeError):
                    pass
            form = wins / len(recent) if recent else None
            fatigue = min(1.0, recent_count / 3.0)   # 3+ matches in a week = maxed
            return form, fatigue

        fa, fata = _player_stats("firstPlayerResults")
        fb, fatb = _player_stats("secondPlayerResults")
        if fa is not None and fb is not None:
            ctx["form_a"], ctx["form_b"] = fa, fb
        if fata is not None and fatb is not None:
            ctx["fatigue_a"], ctx["fatigue_b"] = fata, fatb
        return ctx
