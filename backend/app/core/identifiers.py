"""Deterministic validators for security identifiers."""

import re

_ISIN_SHAPE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def is_valid_isin(isin: str) -> bool:
    """Validate an ISIN (ISO 6166): shape plus the Luhn check digit.

    Letters expand to two digits (A=10 ... Z=35), then the standard Luhn
    algorithm is applied to the resulting digit string.
    """
    if not _ISIN_SHAPE.match(isin):
        return False
    digits = "".join(str(int(ch, 36)) for ch in isin)
    total = 0
    # Walk right-to-left; double every second digit starting from the one next to the check digit.
    for index, ch in enumerate(reversed(digits)):
        d = int(ch)
        if index % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
