"""Text normalisation shared by the alias store and the company resolver."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Legal-form abbreviations that users type interchangeably. Applied to stored aliases AND queries
# (both go through normalize_text), so "Ltd" and "Limited" always compare equal.
# NOTE: changing this function requires re-normalising stored `company_aliases.normalized_alias`.
_CANONICAL_TOKENS = {"ltd": "limited"}


def normalize_text(value: str) -> str:
    """Lower-case, strip accents, treat '&' as 'and', drop punctuation, collapse spaces.

    "Reliance Industries Ltd." -> "reliance industries limited"
    "Hindustan Unilever  & Co" -> "hindustan unilever and co"
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    folded = folded.lower().replace("&", " and ")
    tokens = _NON_ALNUM.sub(" ", folded).split()
    return " ".join(_CANONICAL_TOKENS.get(t, t) for t in tokens)
