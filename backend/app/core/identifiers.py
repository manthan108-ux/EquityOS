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


def isin_check_digit(isin_without_check: str) -> str:
    """Compute the ISO 6166 check digit for the first 11 characters of an ISIN."""
    for digit in "0123456789":
        if is_valid_isin(isin_without_check + digit):
            return digit
    raise ValueError(f"Cannot compute a check digit for {isin_without_check!r}")


def issuer_key(isin: str) -> str:
    """Issuer part of an Indian ISIN (characters 1-7, e.g. 'INE002A').

    Different securities of one issuer (equity, partly-paid, differential-voting shares) share
    this prefix, which is how the NSE and BSE listings of one company are merged.

    ASSUMPTION: securities issued under the 'IN9' prefix (e.g. differential-voting shares) share
    the 4-character issuer code of the same issuer's 'INE' securities, so 'IN9155A' maps to
    'INE155A'. If an exchange file ever contradicts this, the import report will show it.
    """
    if isin.startswith("IN9"):
        return "INE" + isin[3:7]
    return isin[:7]
