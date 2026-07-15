"""
chat_routes.py — Line Logic AI assistant.

Answers ONLY from the model's real predictions for today's slate. It's a
natural-language wrapper around Line Logic's own numbers, not a sports oracle:
the model's picks/probabilities are retrieved server-side and handed to the LLM
as the sole source of truth, with a system prompt that forbids inventing games,
teams, odds, or probabilities.

Requires ANTHROPIC_API_KEY in the environment (Railway). CHAT_MODEL is optional
(defaults to a fast, inexpensive model).
"""
import os
import datetime as dt

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_MODEL = os.environ.get("CHAT_MODEL", "claude-haiku-4-5-20251001")


def _get_key():
    """Read the Anthropic key fresh each call, accepting common var names, so it
    picks up whatever the site already uses without a restart."""
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY", "LLM_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""
_MAX_GAMES = 70
_MAX_HISTORY = 6


class ChatIn(BaseModel):
    message: str
    history: list | None = None
    favorites: list | None = None


_calib_cache = {"t": 0, "line": ""}


def _calib_line():
    """One-line model track record from the calibration data (cached 30 min)."""
    import time
    if time.time() - _calib_cache["t"] < 1800 and _calib_cache["line"]:
        return _calib_cache["line"]
    line = ""
    try:
        import reports
        c = reports.calibration()
        n, brier = c.get("n"), c.get("brier")
        if n:
            line = (f"Model track record: {n} settled picks scored so far, "
                    f"Brier score {brier} (0.25 = a coin flip; lower is sharper).")
    except Exception:
        line = ""
    _calib_cache["t"] = time.time()
    _calib_cache["line"] = line
    return line


def _slate_context(target=None):
    """Compact, factual summary of today's real model predictions."""
    try:
        import main
        plays = main._gather_plays(target or dt.date.today())
    except Exception:
        return "", 0
    lines, n = [], 0
    for p in plays:
        if n >= _MAX_GAMES:
            break
        match = p.get("match")
        pick = p.get("pick")
        prob = p.get("prob")
        if not match or not pick or prob is None:
            continue
        sport = (p.get("sport") or "").upper()
        where = p.get("tournament") or p.get("league") or ""
        pct = round(float(prob) * 100)
        extra = f" [{where}]" if where else ""
        tail = ""
        conf = p.get("confidence")
        if conf:
            tail += f", confidence {conf}"
        edge = p.get("edge")
        if isinstance(edge, (int, float)) and edge:
            tail += f", edge {round(edge * 100) if abs(edge) < 1 else round(edge)}%"
        h2h = p.get("h2h")
        if isinstance(h2h, dict) and h2h.get("record"):
            seas = "last %d seasons" % h2h["seasons"] if h2h.get("seasons", 1) > 1 else "this season"
            tail += f", season series (home team): {h2h['record']} in {h2h.get('games', '?')} mtgs {seas}"
        lines.append(f"- {sport}{extra}: {match} \u2014 model pick: {pick} ({pct}%){tail}")
        n += 1
    return "\n".join(lines), n


_SYSTEM = """You are the Line Logic analyst \u2014 a sharp, friendly sports-analytics assistant for an honest predictions site. You sound like a knowledgeable friend who only deals in real numbers.

You answer ONLY from the DATA below (today's actual model predictions) and from the lookup_team tool. Rules you must follow exactly:
- Use ONLY the picks, probabilities, and figures in the DATA or returned by tools. NEVER invent or estimate a probability, team, matchup, score, record, or odds that isn't there.
- If asked about a game, team, or matchup not in the DATA, say plainly the model doesn't have it today. Do not guess or use outside knowledge for numbers.
- When you give a pick, cite the model's probability (e.g., "the model has the Yankees at 58%").
- Explain the WHY when you can, using the real signals present: confidence, edge, season series (H2H), and \u2014 after a lookup_team call \u2014 the team's form, record, splits, streak, and rating. Tie the reasoning to those actual numbers, never to invented ones.
- "season series (home team): X-Y" is the HOME team's record vs the away team (over the seasons noted). Report it that way if asked who's won the series.
- PLAYER PROPS: you MUST call the lookup_props tool whenever the user asks about props, projections, or which props to bet/ladder/parlay for a game — even if you don't see that game in the DATA summary. The DATA summary lists team-level picks only; props live behind the tool. NEVER tell the user a game or its props don't exist based on the DATA summary alone — the summary is not the full board. Call lookup_props with the team name first, and only report "no props" if the TOOL itself returns none.
- BUILDING A LADDER/PARLAY: rank by the model's real edge (|projection - line|), prefer legs where recent form and the model_context support the same side, and say plainly that correlated legs in one game raise variance. Give the real numbers for each leg.
- NEVER invent a supporting stat. You do NOT have positional defensive splits (e.g. "allows X to small forwards"), pitcher/batter platoon splits, whiff rates, or usage rates. If a stat isn't in the DATA or a tool result, say the model doesn't track it rather than guessing a number. Justify props ONLY with: the projection, the line, the edge, the odds, and the model_context provided.
- Use the lookup_team tool whenever the user asks about a specific team's form/record or to compare two teams; answer only from what it returns. If a team isn't on today's board, say you can only pull profiles for teams playing today.
- If asked how accurate the model is, use the track-record line (Brier / picks scored). Don't overstate it.
- These are model estimates, not guarantees \u2014 never promise a win or give betting/financial advice.

Style:
- Be concise and conversational \u2014 usually 1-4 sentences. Lead with the answer.
- You may use light Markdown: **bold** for the key pick/number, and "- " bullets when listing multiple games. Don't overformat.
- If the user has FAVORITE TEAMS and any are playing today, you can proactively mention how the model sees them.
- You report the model's view; you're not a tipster and have no opinions beyond the data.

DATA (today's model predictions):
{data}
"""


import json as _json
import time as _time

_TEAM_IDX = {"t": 0, "idx": {}}
_PROFILE_SPORTS = ("mlb", "nba", "wnba", "nfl", "nhl", "ncaab", "ncaaf", "wncaab", "soccer")


def _norm_team(s):
    try:
        import name_match
        return name_match._norm(s)
    except Exception:
        return (s or "").strip().lower()


def _team_index():
    """{normalized team name: (sport, team_id, display_name)} for teams playing
    today. Cached 10 min; only in-season sports are polled."""
    if _time.time() - _TEAM_IDX["t"] < 600 and _TEAM_IDX["idx"]:
        return _TEAM_IDX["idx"]
    idx = {}
    try:
        import main
        import datetime as dt
        today = dt.date.today().isoformat()
        mo = dt.date.today().month
        season = getattr(main, "SPORT_SEASON", {})
        for sport in _PROFILE_SPORTS:
            months = season.get(sport)
            if months and mo not in months:
                continue
            try:
                games = main.team_games(sport, today)
            except Exception:
                continue
            rows = games.get("games") if isinstance(games, dict) else games
            for g in rows or []:
                if not isinstance(g, dict):
                    continue
                for side in ("home", "away"):
                    t = g.get(side) or {}
                    tid, nm = t.get("team_id"), t.get("name")
                    if tid and nm:
                        idx[_norm_team(nm)] = (g.get("sport") or sport, tid, nm)
    except Exception:
        idx = {}
    if idx:
        _TEAM_IDX["t"] = _time.time()
        _TEAM_IDX["idx"] = idx
    return idx


def _resolve_team(query):
    idx = _team_index()
    if not idx:
        return None
    q = _norm_team(query)
    if q in idx:
        return idx[q]
    for nm, val in idx.items():
        if q and (q in nm or nm in q):
            return val
    import difflib
    close = difflib.get_close_matches(q, list(idx.keys()), n=1, cutoff=0.8)
    return idx[close[0]] if close else None


def _lookup_team(name):
    """Tool executor: real season profile for a team playing today."""
    try:
        r = _resolve_team(name)
        if not r:
            return {"found": False,
                    "note": f"{name} isn't on today's board, so there's no live profile to pull."}
        sport, tid, tname = r
        import reports
        prof = reports.team_profile(sport, str(tid), tname)
        if not isinstance(prof, dict):
            return {"found": False}
        keep = {k: prof.get(k) for k in
                ("name", "sport", "rating", "record", "home_record", "away_record",
                 "last10", "streak", "form", "ppg", "opp_ppg", "score_term", "adv")
                if prof.get(k) is not None}
        return {"found": True, "profile": keep}
    except Exception as e:
        return {"found": False, "error": str(e)[:150]}


def _lookup_props(team_or_matchup, stat=None):
    """Tool executor: REAL model prop projections vs. book lines for a game on
    today's board. Returns only computed numbers (projection, line, edge, and the
    opponent/pace context the model actually used) \u2014 never invented splits."""
    try:
        r = _resolve_team(team_or_matchup)
        if not r:
            return {"found": False,
                    "note": f"{team_or_matchup} isn't on today's board, so there are no props to pull."}
        sport, tid, tname = r
        if sport not in ("nba", "wnba", "nfl", "mlb"):
            return {"found": False, "note": f"The model doesn't do player props for {sport}."}
        import main as _m
        day = dt.date.today()
        games = _m.team_games(sport, day.isoformat())
        gid = None
        for g in (games.get("games") if isinstance(games, dict) else games) or []:
            for side in ("home", "away"):
                if str((g.get(side) or {}).get("team_id")) == str(tid):
                    gid = g.get("id") or g.get("game_id")
                    break
            if gid:
                break
        if not gid:
            return {"found": False, "note": "Couldn't find that game on today's board."}
        # MLB props live on their own endpoint (team_props only serves nba/wnba/nfl)
        if sport == "mlb":
            data = _m.mlb_props(int(gid), date=day.isoformat())
        else:
            data = _m.team_props(sport, str(gid), date=day.isoformat())
        raw = (data or {}).get("props") or []
        rows = []
        for p in raw:
            label = str(p.get("label") or p.get("stat") or "")
            if stat and (stat or "").lower() not in label.lower():
                continue
            line = p.get("line")
            proj = p.get("projection")
            if proj is None:
                proj = p.get("model_line") or p.get("proj")
            row = {"player": p.get("player"), "team": p.get("team"), "stat": label,
                   "line": line, "model_projection": proj,
                   "over_odds": p.get("over_odds"), "under_odds": p.get("under_odds"),
                   "model_context": p.get("context")}
            try:
                if proj is not None and line is not None:
                    row["diff"] = round(float(proj) - float(line), 2)
                    row["lean"] = "OVER" if float(proj) > float(line) else "UNDER"
            except (TypeError, ValueError):
                pass
            if p.get("actual") is not None:
                row["actual"] = p["actual"]
            rows.append(row)
        rows.sort(key=lambda x: -abs(x.get("diff") or 0))
        if not rows:
            return {"found": False, "sport": sport, "game": tname,
                    "note": ("The board shows no props for that game right now \u2014 books "
                             "usually post player props closer to game time.")}
        return {"found": True, "sport": sport, "game": tname, "props": rows[:16],
                "note": ("model_projection is the model's number; diff = projection - line "
                         "(the edge). Only these figures are real \u2014 do not add outside stats.")}
    except Exception as e:
        return {"found": False, "error": str(e)[:150]}


_TOOLS = [{
    "name": "lookup_team",
    "description": ("Look up a team's REAL season profile for a team playing today: "
                    "power rating, overall record, home/away splits, last-10, current "
                    "streak, recent form, and points/goals/runs for and against. Use "
                    "this whenever the user asks about a specific team's form, record, "
                    "how they've been playing, or how two teams compare."),
    "input_schema": {"type": "object", "properties": {
        "team": {"type": "string", "description": "Team name as the user referred to it, e.g. 'Lakers'."}},
        "required": ["team"]},
}, {
    "name": "lookup_props",
    "description": ("Get the model's REAL player-prop projections vs. the book lines for a "
                    "game on today's board (NBA, WNBA, NFL, MLB). Returns each player's "
                    "line, the model's projection, the difference (edge), the over/under "
                    "odds, and the model's context (opponent defense + pace factors it "
                    "used). Use this whenever the user asks about player props, projections, "
                    "or which props to bet/ladder/parlay."),
    "input_schema": {"type": "object", "properties": {
        "team": {"type": "string", "description": "A team in the game, e.g. 'Lynx' or 'Lakers'."},
        "stat": {"type": "string", "description": "Optional stat filter, e.g. 'points', 'rebounds', 'strikeouts'."}},
        "required": ["team"]},
}]


def _anthropic(messages, system, tools=None):
    import httpx
    payload = {"model": _MODEL, "max_tokens": 700, "system": system, "messages": messages}
    if tools:
        payload["tools"] = tools
    r = httpx.post("https://api.anthropic.com/v1/messages",
                   headers={"x-api-key": _get_key(), "anthropic-version": "2023-06-01",
                            "content-type": "application/json"},
                   json=payload, timeout=40.0)
    r.raise_for_status()
    return r.json()


@router.get("/api/chat/resolve-diag")
def chat_resolve_diag(team: str = "valkyries"):
    """Debug: what teams does the chat see today, and does a name resolve?"""
    try:
        idx = _team_index()
        r = _resolve_team(team)
        return {"team_queried": team, "resolved": r,
                "indexed_teams": sorted(idx.keys()),
                "count": len(idx)}
    except Exception as e:
        return {"error": str(e)[:200]}


@router.get("/api/chat/diag")
def chat_diag(model: str | None = None):
    """Minimal Anthropic call to surface the real failure (status + body). Also
    echoes the raw env so we can see exactly what the running app loaded. Pass
    ?model=... to test a specific model id without redeploying."""
    key = _get_key()
    env_report = {
        "CHAT_MODEL_env": os.environ.get("CHAT_MODEL"),
        "ANTHROPIC_MODEL_env": os.environ.get("ANTHROPIC_MODEL"),
        "resolved_MODEL": _MODEL,
    }
    if not key:
        return {"ok": False, "reason": "no key found", "env": env_report,
                "checked": ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_KEY", "LLM_API_KEY"]}
    use_model = model or _MODEL
    try:
        import httpx
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": use_model, "max_tokens": 16,
                             "messages": [{"role": "user", "content": "ping"}]},
                       timeout=20.0)
        return {"ok": r.status_code == 200, "status": r.status_code,
                "model": use_model, "env": env_report, "key_tail": key[-4:], "body": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "model": use_model,
                "env": env_report, "key_tail": key[-4:]}


@router.post("/api/chat")
def chat(inp: ChatIn):
    if not _get_key():
        return {"reply": "The assistant isn't configured yet \u2014 an ANTHROPIC_API_KEY needs to be set.",
                "error": "no_key"}
    msg = (inp.message or "").strip()
    if not msg:
        return {"reply": "Ask me about today's games \u2014 for example, \u201cwho does the model like in the Yankees game?\u201d"}

    data, n = _slate_context()
    if not n:
        data = "(No games with model predictions on the board right now.)"
    calib = _calib_line()
    if calib:
        data = calib + "\n\n" + data
    favs = []
    for f in (inp.favorites or [])[:20]:
        lbl = (f.get("l") if isinstance(f, dict) else str(f)) or ""
        if lbl:
            favs.append(lbl)
    if favs:
        data = "USER'S FAVORITE TEAMS/PLAYERS: " + ", ".join(favs) + "\n\n" + data

    history = []
    for h in (inp.history or [])[-_MAX_HISTORY:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    history.append({"role": "user", "content": msg})

    try:
        system = _SYSTEM.format(data=data)
        body = _anthropic(history, system, tools=_TOOLS)
        rounds = 0
        while body.get("stop_reason") == "tool_use" and rounds < 2:
            rounds += 1
            history.append({"role": "assistant", "content": body.get("content", [])})
            results = []
            for block in body.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "lookup_props":
                    _in = block.get("input") or {}
                    out = _lookup_props(_in.get("team", ""), _in.get("stat"))
                    results.append({"type": "tool_result", "tool_use_id": block.get("id"),
                                    "content": _json.dumps(out)})
                elif block.get("type") == "tool_use" and block.get("name") == "lookup_team":
                    out = _lookup_team((block.get("input") or {}).get("team", ""))
                    results.append({"type": "tool_result", "tool_use_id": block.get("id"),
                                    "content": _json.dumps(out)})
            if not results:
                break
            history.append({"role": "user", "content": results})
            body = _anthropic(history, system, tools=_TOOLS)
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        reply = "\n".join(x for x in parts if x).strip()
        return {"reply": reply or "Sorry, I couldn't come up with an answer.", "games": n}
    except Exception as e:
        return {"reply": "I couldn't reach the assistant just now. Try again in a moment.",
                "error": str(e)[:200]}
