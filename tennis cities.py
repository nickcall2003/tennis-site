"""
tennis_cities.py — format ITF tournament labels with the host country.

api-tennis embeds the city in the tournament name ("M25 Skopje") but gives no
country field, so betting-site-style "City (Country)" needs a city->country map.

Honesty rule: only cities we're confident about are listed, and genuinely
ambiguous names that exist in several countries (Cordoba, Valencia, Santiago,
Cartagena, San Jose, Victoria, etc.) are deliberately OMITTED so we never label
a tournament with the wrong country. Unknown cities keep their original label.
Extend CITY_COUNTRY as gaps show up.
"""
import re
import unicodedata

CITY_COUNTRY = {
    # --- from the live board ---
    "skopje": "North Macedonia", "getxo": "Spain", "amstelveen": "Netherlands",
    "maanshan": "China", "san diego": "USA", "elvas": "Portugal",
    # --- Turkey (huge ITF hub) ---
    "antalya": "Turkey", "istanbul": "Turkey", "ankara": "Turkey",
    # --- Tunisia (huge ITF hub) ---
    "monastir": "Tunisia", "tunis": "Tunisia", "hammamet": "Tunisia",
    # --- Egypt (huge ITF hub) ---
    "cairo": "Egypt", "sharm elsheikh": "Egypt", "sharm el sheikh": "Egypt",
    "alexandria": "Egypt", "ain sokhna": "Egypt",
    # --- Greece ---
    "heraklion": "Greece", "thessaloniki": "Greece",
    # --- Spain ---
    "madrid": "Spain", "barcelona": "Spain", "sabadell": "Spain", "vigo": "Spain",
    "bakio": "Spain", "vic": "Spain", "manacor": "Spain", "gandia": "Spain",
    # --- Portugal ---
    "lisbon": "Portugal", "lisboa": "Portugal", "porto": "Portugal",
    "faro": "Portugal", "vale do lobo": "Portugal", "idanha-a-nova": "Portugal",
    # --- Italy ---
    "rome": "Italy", "roma": "Italy", "milan": "Italy", "bari": "Italy",
    "santa margherita di pula": "Italy", "trieste": "Italy", "vicenza": "Italy",
    # --- France ---
    "grenoble": "France", "bourg-en-bresse": "France", "poitiers": "France",
    # --- Germany ---
    "nussloch": "Germany", "kaltenkirchen": "Germany", "trier": "Germany",
    # --- Netherlands ---
    "alkmaar": "Netherlands", "the hague": "Netherlands",
    # --- Great Britain ---
    "nottingham": "Great Britain", "glasgow": "Great Britain",
    "roehampton": "Great Britain", "sunderland": "Great Britain",
    # --- USA ---
    "orlando": "USA", "norman": "USA", "wichita": "USA", "claremont": "USA",
    "little rock": "USA", "champaign": "USA", "landisville": "USA",
    "edwardsville": "USA", "rancho santa fe": "USA", "tucson": "USA", "waco": "USA",
    # --- China ---
    "anning": "China", "shenzhen": "China", "nanjing": "China", "guiyang": "China",
    "jinan": "China", "wuhan": "China", "zhuhai": "China", "tianjin": "China",
    # --- Mexico ---
    "cancun": "Mexico", "villahermosa": "Mexico", "aguascalientes": "Mexico",
    # --- Australia ---
    "mildura": "Australia", "cairns": "Australia", "traralgon": "Australia",
    # --- Japan ---
    "kashiwa": "Japan", "tokyo": "Japan",
    # --- India ---
    "new delhi": "India", "pune": "India", "chandigarh": "India",
    # --- Croatia ---
    "zagreb": "Croatia", "bol": "Croatia",
    # --- Others (unambiguous) ---
    "sofia": "Bulgaria", "bratislava": "Slovakia", "brno": "Czechia",
    "prague": "Czechia", "budapest": "Hungary", "bucharest": "Romania",
    "astana": "Kazakhstan", "nur-sultan": "Kazakhstan", "doha": "Qatar",
    "kigali": "Rwanda", "nairobi": "Kenya", "luanda": "Angola",
}

_GRADE = re.compile(r"^([MW]\d{2,3})\s+(.*)$")


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().strip())


def format_tournament(name):
    """Return the tournament label with the host country appended, e.g.
    'M25 Skopje' -> 'M25 Skopje (North Macedonia)'. Leaves the name unchanged if
    it already has a parenthetical, isn't an ITF grade name, or the city is
    unknown/ambiguous."""
    if not name:
        return name
    n = name.strip()
    if "(" in n and ")" in n:            # already has a country/parenthetical
        return name
    m = _GRADE.match(n)
    if not m:                            # not an "M/W<level> City" ITF label
        return name
    grade, rest = m.group(1), m.group(2).strip()
    key = re.sub(r",\s*[A-Za-z]{2}$", "", rest)   # drop ", CA" US-state suffix
    key = re.sub(r"\s+\d+$", "", key).strip()     # drop trailing series number
    country = CITY_COUNTRY.get(_norm(key))
    return f"{grade} {rest} ({country})" if country else name
