"""
names.py - Player name normalization + fuzzy matching for joining sources.

Used when merging keeper names and ESPN (or other site) baselines with the
FantasyPros pulls. Handles suffixes (Jr./Sr./II/III/IV/V), punctuation,
defense/special-teams naming (D/ST, DST, "Bears D/ST"), and common
abbreviations ("JSN" -> Jaxon Smith-Njigba, "Amon Ra" -> Amon-Ra St. Brown)
via a manual override map.
"""

import re
import unicodedata
from typing import Dict, Iterable, Optional, Tuple

from rapidfuzz import fuzz, process

SUFFIXES = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'v'}

DST_PATTERN = re.compile(r'\b(d\s*/?\s*st|dst|defense|def)\b')

# Manual overrides: normalized alias -> canonical full name.
MANUAL_OVERRIDES: Dict[str, str] = {
    'jsn': 'Jaxon Smith-Njigba',
    'amon ra': 'Amon-Ra St. Brown',
    'amon ra st brown': 'Amon-Ra St. Brown',
    'st brown': 'Amon-Ra St. Brown',
    'matt stafford': 'Matthew Stafford',
    'ceedee': 'CeeDee Lamb',
    'cmc': 'Christian McCaffrey',
    'jt': 'Jonathan Taylor',
    'aj brown': 'A.J. Brown',
    'dj moore': 'D.J. Moore',
    'tj hockenson': 'T.J. Hockenson',
    'dk metcalf': 'DK Metcalf',
    'hollywood brown': 'Marquise Brown',
    'kenneth walker': 'Kenneth Walker III',
    'marvin harrison': 'Marvin Harrison Jr.',
    'brian robinson': 'Brian Robinson Jr.',
    'travis etienne': 'Travis Etienne Jr.',
    'michael pittman': 'Michael Pittman Jr.',
    'gabe davis': 'Gabriel Davis',
    'josh palmer': 'Joshua Palmer',
    'cam ward': 'Cameron Ward',
    'chig okonkwo': 'Chigoziem Okonkwo',
}


def normalize_name(name: Optional[str]) -> str:
    """
    Lowercase, strip accents/punctuation/suffixes and team-defense noise.

    'Amon-Ra St. Brown' -> 'amon ra st brown'
    'Travis Etienne Jr.' -> 'travis etienne'
    'Chicago Bears D/ST' -> 'chicago bears dst'
    """
    if name is None:
        return ''
    text = unicodedata.normalize('NFKD', str(name))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = DST_PATTERN.sub('dst', text)
    text = re.sub(r"[.'`\u2019]", '', text)
    text = re.sub(r'[-/,]', ' ', text)
    text = re.sub(r'[^a-z0-9 ]', ' ', text)
    tokens = [t for t in text.split() if t]
    if 'dst' not in tokens:
        while tokens and tokens[-1] in SUFFIXES:
            tokens.pop()
    return ' '.join(tokens)


def canonical_name(name: Optional[str]) -> str:
    """Apply the manual override map (by normalized key), else return the input."""
    norm = normalize_name(name)
    return MANUAL_OVERRIDES.get(norm, name if name is not None else '')


def normalized_key(name: Optional[str]) -> str:
    """Normalized join key with manual overrides resolved first."""
    return normalize_name(canonical_name(name))


def build_index(names: Iterable[str]) -> Dict[str, str]:
    """Map normalized key -> original name for a collection of source names."""
    return {normalized_key(n): n for n in names if n}


def match_name(name: str, index: Dict[str, str], threshold: int = 88) -> Tuple[Optional[str], float]:
    """
    Resolve `name` against an index built by build_index().

    Returns (matched_original_name, score). Exact normalized hits score 100.
    Falls back to rapidfuzz token-set matching above `threshold`, else (None, score).
    """
    key = normalized_key(name)
    if not key:
        return None, 0.0
    if key in index:
        return index[key], 100.0
    if not index:
        return None, 0.0
    best = process.extractOne(key, list(index.keys()), scorer=fuzz.token_set_ratio)
    if best and best[1] >= threshold:
        return index[best[0]], float(best[1])
    return None, float(best[1]) if best else 0.0
