"""
name_match.py — match our ESPN team names to another provider's team names/IDs.

Different data providers key everything by their own internal IDs, and their
team names rarely match ESPN's exactly ("Man United" vs "Manchester United").
This normalizes both sides and does a best-effort match, with a manual override
map you can extend when the fuzzy match gets one wrong. Honest by design: if we
can't confidently match, we return None rather than guess and show wrong data.
"""
import difflib
import unicodedata

# Manual overrides: normalized ESPN name -> provider name (extend as needed).
# Keep keys lowercased/normalized (run _norm on the ESPN name to get the key).
OVERRIDES = {
    # "man utd": "manchester united",
    # "spurs": "tottenham hotspur",
}

_DROP = (" fc", " afc", " cf", " sc", " ac", " calcio", " club")


def _norm(s):
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.replace("&", "and").replace(".", "").replace("'", "")
    s = s.replace("united", "utd")
    for d in _DROP:
        if s.endswith(d):
            s = s[: -len(d)]
    return " ".join(s.split())


def build_index(items, name_key="name", id_key="id"):
    """Given a provider's team list (dicts), return {normalized_name: id}."""
    idx = {}
    for it in items or []:
        nm = it.get(name_key) if isinstance(it, dict) else None
        if nm is None:
            continue
        idx[_norm(nm)] = it.get(id_key) if isinstance(it, dict) else None
    return idx


def match(espn_name, index, cutoff=0.86):
    """Resolve an ESPN name to a provider id via override -> exact -> fuzzy.
    Returns the id or None. `index` is {normalized_provider_name: id}."""
    key = _norm(espn_name)
    if key in OVERRIDES:
        key = _norm(OVERRIDES[key])
    if key in index:
        return index[key]
    # substring containment either direction (handles "man utd" vs "manchester utd")
    for nm, _id in index.items():
        if key and (key in nm or nm in key) and abs(len(key) - len(nm)) <= 6:
            return _id
    close = difflib.get_close_matches(key, list(index.keys()), n=1, cutoff=cutoff)
    return index[close[0]] if close else None
