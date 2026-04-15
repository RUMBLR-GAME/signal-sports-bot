"""
teams.py — Robust Team Name Matching
Handles all known naming differences between ESPN and Polymarket.

ESPN says "Wolverhampton Wanderers", Polymarket says "Wolves".
ESPN says "Paris Saint-Germain", Polymarket says "PSG".
ESPN says "Atlético Madrid", Polymarket says "Atletico Madrid".

Approach:
  1. Normalize: lowercase, strip suffixes (FC, CF, SC, AFC, etc.)
  2. Remove accents: São → Sao, Atlético → Atletico, München → Munchen
  3. Known alias map: hardcoded for the tricky ones
  4. Token matching: split into words, check overlap
  5. generate_search_terms(): returns all possible match strings for a team

Exports:
    normalize(name) → str
    generate_search_terms(full_name, abbrev) → list[str]
    teams_match(name_a, name_b) → bool
"""

import unicodedata
import re

# ─── SUFFIX STRIPPING ────────────────────────────────────────────────────────
# These appear inconsistently between ESPN and Polymarket
_SUFFIXES = {
    " fc", " cf", " sc", " afc", " ssc", " ac", " as", " us",
    " fk", " sk", " bk", " if", " gk",  # Nordic
    " cd", " ud", " sd",                  # Spanish
    " rcd", " rc",                         # Spanish
    " bsc", " bsv",                        # German
    " 1899", " 1904", " 04", " 09",       # Year suffixes
}


# ─── KNOWN ALIASES ───────────────────────────────────────────────────────────
# Maps normalized ESPN names → additional Polymarket search terms
# This is the critical list that makes matching work
_ALIASES = {
    # English
    "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "tottenham hotspur": ["spurs", "tottenham"],
    "brighton hove albion": ["brighton", "brighton & hove"],
    "brighton and hove albion": ["brighton"],
    "west ham united": ["west ham"],
    "nottingham forest": ["nott'm forest", "nottm forest", "forest"],
    "sheffield united": ["sheffield utd", "sheff utd"],
    "sheffield wednesday": ["sheffield wed", "sheff wed"],
    "queens park rangers": ["qpr"],
    "west bromwich albion": ["west brom"],
    "afc bournemouth": ["bournemouth"],
    "newcastle united": ["newcastle"],
    "manchester city": ["man city", "man. city"],
    "manchester united": ["man utd", "man. utd", "man united"],
    "leeds united": ["leeds"],
    "leicester city": ["leicester"],
    "crystal palace": ["palace"],
    # French
    "paris saint-germain": ["psg", "paris sg", "paris saint germain"],
    "paris saint germain": ["psg", "paris sg"],
    "olympique marseille": ["marseille", "om"],
    "olympique lyonnais": ["lyon", "ol"],
    "as monaco": ["monaco"],
    "stade rennais": ["rennes"],
    # German
    "bayern munich": ["bayern munchen", "bayern münchen", "fc bayern"],
    "borussia dortmund": ["bvb", "dortmund"],
    "borussia monchengladbach": ["gladbach", "monchengladbach", "borussia mgladbach"],
    "bayer leverkusen": ["leverkusen", "bayer 04"],
    "rb leipzig": ["leipzig", "rasenballsport leipzig"],
    "eintracht frankfurt": ["frankfurt", "eintracht"],
    "vfb stuttgart": ["stuttgart"],
    "vfl wolfsburg": ["wolfsburg"],
    # Spanish
    "atletico madrid": ["atletico", "atlético madrid", "atl madrid", "atl. madrid"],
    "atletico de madrid": ["atletico", "atletico madrid"],
    "real betis balompie": ["real betis", "betis"],
    "real sociedad": ["sociedad", "real sociedad de futbol"],
    "real valladolid": ["valladolid"],
    "deportivo alaves": ["alaves", "alavés"],
    "celta de vigo": ["celta vigo", "celta"],
    "rayo vallecano": ["rayo"],
    # Italian
    "internazionale": ["inter milan", "inter"],
    "fc internazionale milano": ["inter milan", "inter"],
    "as roma": ["roma"],
    "ac milan": ["milan", "ac milan"],
    "ssc napoli": ["napoli"],
    "atalanta bc": ["atalanta"],
    "juventus fc": ["juventus", "juve"],
    "us lecce": ["lecce"],
    "hellas verona": ["verona"],
    # Portuguese
    "sporting cp": ["sporting lisbon", "sporting"],
    "sl benfica": ["benfica"],
    "fc porto": ["porto"],
    "sporting clube de braga": ["braga", "sc braga"],
    # Dutch
    "ajax amsterdam": ["ajax"],
    "psv eindhoven": ["psv"],
    "feyenoord rotterdam": ["feyenoord"],
    "az alkmaar": ["az"],
    # Turkish
    "galatasaray sk": ["galatasaray", "gala"],
    "fenerbahce sk": ["fenerbahce", "fener"],
    "besiktas jk": ["besiktas"],
    # South American
    "sao paulo": ["são paulo", "sao paulo fc"],
    "club america": ["club américa", "america"],
    "boca juniors": ["boca"],
    "river plate": ["river"],
    "flamengo": ["cr flamengo"],
    "palmeiras": ["se palmeiras"],
    "santos": ["santos fc"],
    "gremio": ["grêmio"],
    "atletico mineiro": ["atlético mineiro", "galo"],
    # Asian
    "yokohama f. marinos": ["yokohama marinos", "f. marinos", "marinos"],
    "kawasaki frontale": ["kawasaki", "frontale"],
    "urawa red diamonds": ["urawa reds", "urawa"],
    "jeonbuk hyundai motors": ["jeonbuk", "jeonbuk motors"],
    # US
    "la clippers": ["los angeles clippers", "clippers"],
    "la lakers": ["los angeles lakers", "lakers"],
    "la galaxy": ["los angeles galaxy", "galaxy"],
    "lafc": ["los angeles fc", "los angeles football club"],
    "portland trail blazers": ["trail blazers", "blazers"],
    "minnesota timberwolves": ["timberwolves", "wolves"],
    # Australian
    "melbourne victory": ["victory"],
    "melbourne city": ["melb city"],
    "sydney fc": ["sydney"],
    "western sydney wanderers": ["ws wanderers", "wsw"],
    "central coast mariners": ["mariners", "ccm"],
    "brisbane roar": ["roar"],
    "perth glory": ["perth"],
    "adelaide united": ["adelaide"],
    "wellington phoenix": ["phoenix"],
    "macarthur fc": ["macarthur", "bulls"],
}


def _strip_accents(s: str) -> str:
    """Remove diacritics: São → Sao, Atlético → Atletico, München → Munchen."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """
    Normalize a team name for matching.
    Lowercase, strip accents, remove common suffixes, clean punctuation.
    """
    s = _strip_accents(name.lower().strip())

    # Remove punctuation except hyphens and apostrophes inside words
    s = re.sub(r"[&]", "and", s)
    s = re.sub(r"[^\w\s'-]", "", s)

    # Strip common suffixes
    for suffix in _SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    return s


def _get_tokens(name: str) -> set[str]:
    """Split normalized name into meaningful tokens (skip tiny words)."""
    return {t for t in normalize(name).split() if len(t) > 2}


def generate_search_terms(full_name: str, abbrev: str = "") -> list[str]:
    """
    Generate all possible search terms for a team.
    Used to match against Polymarket market questions and outcomes.

    Returns list of lowercase strings, deduplicated, most specific first.
    """
    terms = []
    norm = normalize(full_name)
    terms.append(norm)

    # Original lowercase
    terms.append(full_name.lower().strip())

    # Accent-stripped lowercase (different from normalize which also strips suffixes)
    terms.append(_strip_accents(full_name.lower().strip()))

    # Last word (usually the mascot/nickname)
    parts = norm.split()
    if len(parts) > 1:
        terms.append(parts[-1])

    # Abbreviation
    if abbrev:
        terms.append(abbrev.lower().strip())

    # Known aliases — check both original and normalized
    for key, aliases in _ALIASES.items():
        if key == norm or key == full_name.lower().strip() or key == _strip_accents(full_name.lower().strip()):
            terms.extend(aliases)
            break

    # City abbreviations for multi-word US cities
    city_shorts = {
        "new york": "ny", "new jersey": "nj", "los angeles": "la",
        "san francisco": "sf", "san antonio": "sa", "golden state": "gs",
        "oklahoma city": "okc", "tampa bay": "tb", "green bay": "gb",
        "kansas city": "kc", "new orleans": "no", "new england": "ne",
    }
    fl = full_name.lower().strip()
    for city, short in city_shorts.items():
        if fl.startswith(city):
            mascot = fl.replace(city, "").strip()
            if mascot:
                terms.append(f"{short} {mascot}")
            break

    # Deduplicate preserving order, filter empty
    seen = set()
    result = []
    for t in terms:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    return result


def teams_match(name_a: str, name_b: str) -> bool:
    """
    Check if two team names refer to the same team.
    Uses normalization, aliases, and token overlap.

    More lenient than exact matching but requires meaningful overlap.
    """
    norm_a = normalize(name_a)
    norm_b = normalize(name_b)

    # Exact normalized match
    if norm_a == norm_b:
        return True

    # One contains the other
    if norm_a in norm_b or norm_b in norm_a:
        return True

    # Check aliases
    terms_a = set(generate_search_terms(name_a))
    terms_b = set(generate_search_terms(name_b))

    # Any alias overlap
    if terms_a & terms_b:
        return True

    # Token overlap — require at least one meaningful shared token
    tokens_a = _get_tokens(name_a)
    tokens_b = _get_tokens(name_b)
    shared = tokens_a & tokens_b

    # Filter out common noise words
    noise = {"the", "and", "united", "city", "real", "club", "sporting", "athletic"}
    meaningful = shared - noise

    if meaningful:
        return True

    return False


def find_team_in_text(team_name: str, abbrev: str, text: str) -> bool:
    """
    Check if any search term for a team appears in a text string.
    Used for matching against Polymarket market questions.
    """
    text_lower = text.lower()
    for term in generate_search_terms(team_name, abbrev):
        if len(term) > 2 and term in text_lower:
            return True
    return False


def find_team_in_outcomes(team_name: str, abbrev: str, outcomes: list[str]) -> int:
    """
    Find which outcome index matches a team. Returns index or -1.
    Checks all aliases and search terms against each outcome.
    """
    terms = generate_search_terms(team_name, abbrev)
    for i, outcome in enumerate(outcomes):
        outcome_lower = outcome.lower()
        for term in terms:
            if len(term) > 2 and term in outcome_lower:
                return i
    return -1
