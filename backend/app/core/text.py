"""Text normalisation shared by the alias store and the company resolver."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_text(value: str) -> str:
    """Lower-case, strip accents, treat '&' as 'and', drop punctuation, collapse spaces.

    "Reliance Industries Ltd." -> "reliance industries ltd"
    "Hindustan Unilever  & Co" -> "hindustan unilever and co"
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    folded = folded.lower().replace("&", " and ")
    return _NON_ALNUM.sub(" ", folded).strip()
