import pytest

from app.core.identifiers import is_valid_isin
from app.core.text import normalize_text


@pytest.mark.parametrize(
    "isin",
    [
        "INE002A01018",
        "INE467B01029",
        "INE009A01021",
        "INE040A01034",
        "INE090A01021",
        "US0378331005",
    ],
)
def test_valid_isins(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize(
    "isin",
    [
        "INE002A01019",  # wrong check digit
        "INE002A0101",  # too short
        "INE002A010180",  # too long
        "ine002a01018",  # lower case
        "1NE002A01018",  # country code must be letters
        "",
    ],
)
def test_invalid_isins(isin):
    assert not is_valid_isin(isin)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Reliance Industries Ltd.", "reliance industries ltd"),
        ("  HDFC   Bank ", "hdfc bank"),
        ("Hindustan Unilever & Co", "hindustan unilever and co"),
        ("Larsen&Toubro", "larsen and toubro"),
        ("Nestlé India", "nestle india"),
        ("M&M", "m and m"),
        ("", ""),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected
