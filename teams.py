"""
teams.py — Robust Team Name Matching (v18)

Fixes the v17 AC Milan ↔ Inter Milan false-positive.
Uses a "strong alias" tag: explicit alias entries (Wolverhampton → wolves)
bypass the ambiguity filter; implicit last-word matches do not.
"""
import re
import unicodedata


_SUFFIXES = [
    " fc", " cf", " sc", " afc", " ssc", " ac", " as", " us",
    " fk", " sk", " bk", " if", " gk",
    " cd", " ud", " sd", " rcd", " rc",
    " bsc", " bsv",
    " 1899", " 1904", " 04", " 09",
]

_ALIASES = {
    # English
    "wolverhampton wanderers": ["wolves", "wolverhampton"],
    "tottenham hotspur": ["spurs", "tottenham"],
    "brighton hove albion": ["brighton"],
    "brighton and hove albion": ["brighton"],
    "west ham united": ["west ham"],
    "nottingham forest": ["nott'm forest", "nottm forest"],
    "sheffield united": ["sheffield utd", "sheff utd"],
    "sheffield wednesday": ["sheffield wed", "sheff wed"],
    "queens park rangers": ["qpr"],
    "west bromwich albion": ["west brom"],
    "afc bournemouth": ["bournemouth"],
    "newcastle united": ["newcastle"],
    "manchester city": ["man city", "man. city"],
    "manchester united": ["man utd", "man united", "man. utd"],
    "leeds united": ["leeds"],
    "leicester city": ["leicester"],
    "crystal palace": ["palace"],
    # French
    "paris saint-germain": ["psg", "paris sg", "paris saint germain"],
    "paris saint germain": ["psg", "paris sg"],
    "olympique marseille": ["marseille"],
    "olympique lyonnais": ["lyon"],
    "as monaco": ["monaco"],
    "stade rennais": ["rennes"],
    # German
    "bayern munich": ["bayern munchen", "bayern münchen", "fc bayern"],
    "borussia dortmund": ["bvb", "dortmund"],
    "borussia monchengladbach": ["gladbach", "monchengladbach"],
    "bayer leverkusen": ["leverkusen", "bayer 04"],
    "rb leipzig": ["leipzig"],
    "eintracht frankfurt": ["frankfurt", "eintracht"],
    "vfb stuttgart": ["stuttgart"],
    "vfl wolfsburg": ["wolfsburg"],
    # Spanish
    "atletico madrid": ["atletico", "atl madrid", "atl. madrid"],
    "atletico de madrid": ["atletico", "atletico madrid"],
    "real betis balompie": ["real betis", "betis"],
    "real sociedad": ["sociedad"],
    "real valladolid": ["valladolid"],
    "deportivo alaves": ["alaves"],
    "celta de vigo": ["celta vigo", "celta"],
    "rayo vallecano": ["rayo"],
    # Italian — the AC/Inter case
    "internazionale": ["inter milan", "inter"],
    "fc internazionale milano": ["inter milan", "inter"],
    "as roma": ["roma"],
    "ac milan": ["milan"],
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
    # Americas
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
    "yokohama f. marinos": ["yokohama marinos", "marinos"],
    "kawasaki frontale": ["kawasaki", "frontale"],
    "urawa red diamonds": ["urawa reds", "urawa"],
    "jeonbuk hyundai motors": ["jeonbuk"],
    # US
    "la clippers": ["los angeles clippers", "clippers"],
    "la lakers": ["los angeles lakers", "lakers"],
    "la galaxy": ["los angeles galaxy", "galaxy"],
    "lafc": ["los angeles fc"],
    "portland trail blazers": ["trail blazers", "blazers"],
    "minnesota timberwolves": ["timberwolves"],
    "new york rangers": ["ny rangers"],
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
    "macarthur fc": ["macarthur"],
}

# Tokens unsafe as sole match evidence unless from strong alias
_AMBIGUOUS_SOLO = {
    "milan", "wanderers", "wolves", "rangers", "united", "city", "real",
    "athletic", "sporting", "fc", "cf", "sc", "club", "the", "madrid",
}


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(name: str) -> str:
    s = _strip_accents((name or "").lower().strip())
    s = re.sub(r"[&]", "and", s)
    s = re.sub(r"[^\w\s'-]", "", s)
    for suffix in _SUFFIXES:
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(name: str) -> set:
    return {t for t in normalize(name).split() if len(t) > 2}


def _word_match(term: str, text: str) -> bool:
    return bool(re.search(r"(?:^|\W)" + re.escape(term) + r"(?:\W|$)", text))


def _generate_terms(full_name: str, abbrev: str = "") -> list:
    """Return [(term, is_strong)] pairs. Strong terms bypass ambiguity."""
    if not full_name:
        return []
    out = []
    norm = normalize(full_name)
    raw_lower = full_name.lower().strip()
    stripped = _strip_accents(raw_lower)

    out.append((norm, False))
    out.append((raw_lower, False))
    if stripped != raw_lower:
        out.append((stripped, False))
    parts = norm.split()
    if len(parts) > 1:
        out.append((parts[-1], False))
    if abbrev:
        out.append((abbrev.lower().strip(), False))

    # Strong aliases — explicit entries
    for key, aliases in _ALIASES.items():
        if key in (norm, raw_lower, stripped):
            for a in aliases:
                out.append((a, True))
            break

    # City shorts (weak)
    city_shorts = {
        "new york": "ny", "new jersey": "nj", "los angeles": "la",
        "san francisco": "sf", "san antonio": "sa", "golden state": "gs",
        "oklahoma city": "okc", "tampa bay": "tb", "green bay": "gb",
        "kansas city": "kc", "new orleans": "no", "new england": "ne",
    }
    for city, short in city_shorts.items():
        if raw_lower.startswith(city):
            mascot = raw_lower.replace(city, "").strip()
            if mascot:
                out.append((f"{short} {mascot}", False))
            break

    # Dedupe — upgrade to strong if any entry is strong
    seen = {}
    for term, strong in out:
        t = term.strip()
        if not t:
            continue
        seen[t] = seen.get(t, False) or strong
    return list(seen.items())


def generate_search_terms(full_name: str, abbrev: str = "") -> list:
    """Back-compat: flat list of terms."""
    return [t for t, _ in _generate_terms(full_name, abbrev)]


def _term_in_text(term: str, is_strong: bool, text: str) -> bool:
    if not term or len(term) <= 2:
        return False
    # Ambiguous solo tokens: strong alias bypass allows match, but only as whole word
    if term in _AMBIGUOUS_SOLO:
        if not is_strong:
            return False
        return _word_match(term, text)
    # Short single-word terms need whole-word boundaries
    if " " not in term and len(term) <= 5:
        return _word_match(term, text)
    return term in text


def find_team_in_text(team_name: str, abbrev: str, text: str) -> bool:
    text_lower = (text or "").lower()
    terms = _generate_terms(team_name, abbrev)
    terms.sort(key=lambda ts: (-len(ts[0].split()), -len(ts[0])))
    for term, strong in terms:
        if _term_in_text(term, strong, text_lower):
            return True
    return False


def find_team_in_outcomes(team_name: str, abbrev: str, outcomes: list) -> int:
    if not outcomes:
        return -1
    terms = _generate_terms(team_name, abbrev)
    terms.sort(key=lambda ts: (-len(ts[0].split()), -len(ts[0])))
    team_distinct_tokens = _tokens(team_name) - _AMBIGUOUS_SOLO

    best_idx = -1
    best_score = 0
    for i, outcome in enumerate(outcomes):
        ol = (outcome or "").lower()
        outcome_distinct = _tokens(outcome) - _AMBIGUOUS_SOLO
        for term, strong in terms:
            if not _term_in_text(term, strong, ol):
                continue
            # If the ONLY match is on an ambiguous token (e.g. "milan"),
            # require at least one distinct token to also appear, OR
            # require that the outcome has no OTHER distinct token that
            # would belong to a different team.
            if term in _AMBIGUOUS_SOLO:
                # Are there distinct tokens in both? If team has e.g. "ac"
                # as distinct token, require "ac" to appear in outcome.
                # If team has no distinct tokens beyond ambiguous ones,
                # we accept (rare — pure-ambiguous names).
                if team_distinct_tokens:
                    # Check if ANY team-distinct token appears in outcome
                    if not any(t in ol for t in team_distinct_tokens):
                        continue
                # Also reject if outcome has a distinct token NOT from our team
                # (means outcome is a different team sharing the ambiguous word)
                foreign = outcome_distinct - team_distinct_tokens
                if foreign and team_distinct_tokens:
                    # Outcome has another team's distinctive tokens
                    continue
            score = len(term)
            if score > best_score:
                best_idx = i
                best_score = score
            break
    return best_idx


def match_game_to_market(home_team, home_abbrev, away_team, away_abbrev,
                         market_question, outcomes):
    hi = find_team_in_outcomes(home_team, home_abbrev, outcomes)
    ai = find_team_in_outcomes(away_team, away_abbrev, outcomes)
    if hi < 0 or ai < 0 or hi == ai:
        return (-1, -1)
    if find_team_in_outcomes(home_team, home_abbrev, [outcomes[ai]]) >= 0:
        return (-1, -1)
    if find_team_in_outcomes(away_team, away_abbrev, [outcomes[hi]]) >= 0:
        return (-1, -1)
    return (hi, ai)


def teams_match(a, b):
    na, nb = normalize(a), normalize(b)
    if na == nb or na in nb or nb in na:
        return True
    ta = set(generate_search_terms(a))
    tb = set(generate_search_terms(b))
    if ta & tb:
        return True
    toka = _tokens(a) - _AMBIGUOUS_SOLO
    tokb = _tokens(b) - _AMBIGUOUS_SOLO
    return bool(toka & tokb)
