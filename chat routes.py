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

_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL = os.environ.get("CHAT_MODEL", "claude-3-5-haiku-latest")
_MAX_GAMES = 70
_MAX_HISTORY = 6


class ChatIn(BaseModel):
    message: str
    history: list | None = None


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
        lines.append(f"- {sport}{extra}: {match} \u2014 model pick: {pick} ({pct}%)")
        n += 1
    return "\n".join(lines), n


_SYSTEM = """You are the Line Logic assistant. Line Logic is an honest sports-analytics site that publishes model predictions.

You answer ONLY from the DATA below, which lists today's actual model predictions. Rules you must follow exactly:
- Use ONLY the picks and probabilities in the DATA. NEVER invent or estimate a probability, team, matchup, score, or odds that isn't in the DATA.
- If asked about a game, player, or matchup not in the DATA, say plainly that the model doesn't have a prediction for it today. Do not guess.
- When you give a pick, cite the model's probability from the DATA (e.g., "the model has the Yankees at 58%").
- These are model estimates, not guarantees. Never promise a win or give financial/betting advice; you report what the model says.
- Be concise, friendly, and factual. A sentence or two is usually enough.
- You represent the model's view; you are not a tipster and you don't have opinions beyond the data.

DATA (today's model predictions):
{data}
"""


@router.post("/api/chat")
def chat(inp: ChatIn):
    if not _KEY:
        return {"reply": "The assistant isn't configured yet \u2014 an ANTHROPIC_API_KEY needs to be set.",
                "error": "no_key"}
    msg = (inp.message or "").strip()
    if not msg:
        return {"reply": "Ask me about today's games \u2014 for example, \u201cwho does the model like in the Yankees game?\u201d"}

    data, n = _slate_context()
    if not n:
        data = "(No games with model predictions on the board right now.)"

    history = []
    for h in (inp.history or [])[-_MAX_HISTORY:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    history.append({"role": "user", "content": msg})

    try:
        import httpx
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": _MODEL, "max_tokens": 600,
                  "system": _SYSTEM.format(data=data), "messages": history},
            timeout=30.0)
        r.raise_for_status()
        body = r.json()
        parts = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
        reply = "\n".join(x for x in parts if x).strip()
        return {"reply": reply or "Sorry, I couldn't come up with an answer.", "games": n}
    except Exception as e:
        return {"reply": "I couldn't reach the assistant just now. Try again in a moment.",
                "error": str(e)[:200]}
