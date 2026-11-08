"""Issuer confirmation for long reports whose cover is artwork, and a guard against a recurring
tooling trap (control characters where a regex escape was meant)."""

from pathlib import Path
from types import SimpleNamespace

from app.docintel.entity import (
    check_entity,
    cover_text,
    document_dominance,
)

NAMES = {"tech mahindra", "tech mahindra limited"}


def pages(texts):
    return [SimpleNamespace(text=t) for t in texts]


def test_no_control_characters_in_application_source():
    """A `\b` written through a shell heredoc becomes a backspace; regexes then silently change."""
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            offenders.append(str(path.relative_to(app_dir)))
    assert offenders == []


def long_report(own_mentions, other_mentions, n_pages=100, other="Beta Industries Limited"):
    body = [f"Tech Mahindra Limited reported results page {i}." for i in range(own_mentions)]
    body += [f"{other} is a subsidiary." for _ in range(other_mentions)]
    filler = ["Standalone balance sheet 1,234"] * (n_pages - len(body))
    return pages([*body, *filler])


def test_a_long_report_dominated_by_the_company_is_its_own():
    note = document_dominance(long_report(25, 3), NAMES)
    assert note and "named 25 times" in note and "next entity 3" in note


def test_a_document_that_merely_quotes_the_company_does_not_pass():
    assert document_dominance(long_report(16, 40), NAMES) is None  # the other entity is named more
    assert document_dominance(long_report(16, 6), NAMES) is None  # not 3x clear of it
    assert document_dominance(long_report(10, 0), NAMES) is None  # too few mentions
    assert document_dominance(long_report(25, 0, n_pages=40), NAMES) is None  # short document
    assert document_dominance(long_report(25, 0), set()) is None


def test_dominance_confirms_a_statement_page_that_names_nobody():
    page = "Standalone Balance Sheet\n(` in Million)\nAs at March 31, 2026"
    assert not check_entity(page, "Integrated Annual Report 2025-26", NAMES).confirmed
    result = check_entity(
        page, "Integrated Annual Report 2025-26", NAMES, dominance="named 25 times"
    )
    assert result.confirmed and result.how.startswith("document subject")


def test_dominance_never_overrides_a_page_that_names_another_entity():
    page = "Beta Services Limited\nStandalone Balance Sheet"
    result = check_entity(page, "cover", NAMES, dominance="named 25 times")
    assert not result.confirmed and "another entity" in result.how


def test_an_image_cover_widens_the_window_only_when_the_name_is_repeated():
    empty = [""] * 3
    named = [f"Tech Mahindra {i}" for i in range(6)]
    assert "Tech Mahindra" in cover_text(pages([*empty, *named]), NAMES)
    assert cover_text(pages([*empty, "Tech Mahindra once", "nothing"]), NAMES) == "\n".join(empty)
    # a cover that has text is never widened
    first = ["Annual Report 2025-26", "Contents", "Foreword"]
    assert cover_text(pages([*first, *named]), NAMES) == "\n".join(first)


def test_exchanges_named_as_letter_addressees_are_not_the_issuer():
    letter = (
        "AXIS/CO/CS/249/2026-27\nJuly 18, 2026\nNational Stock Exchange of India Limited\n"
        "Exchange Plaza\nBSE Limited\nP. J. Towers\nDear Sir/Madam,"
    )
    axis = {"axis bank", "axis bank limited"}
    page = "Statement of financial results\nPage 3"
    assert check_entity(page, "", axis, first_page_text=letter).how.startswith("neither")
    # the letter is neutral; the cover (names the bank) then confirms
    assert check_entity(page, "Axis Bank Limited results", axis, first_page_text=letter).confirmed
    # but a letter from a DIFFERENT issuer still blocks
    other = "Beta Industries Limited\nTo: BSE Limited"
    assert "issued by another entity" in check_entity(page, "", axis, first_page_text=other).how


def test_font_encoding_repairs_for_the_rupee_sign_and_a_dropped_letter():
    from datetime import date

    from app.docintel.detect import find_units
    from app.docintel.periods import parse_column_header

    assert find_units("Consolidated Balance Sheet (H crore) As at 31 March 2026") == {"crore"}
    assert find_units("(H crores)") == {"crore"}
    assert find_units("H crore of loans were repaid") == set()  # not in brackets: not a unit
    period = parse_column_header("ear ended March 31, 2026")
    assert (period.period_end, period.duration_months) == (date(2026, 3, 31), 12)
    assert parse_column_header("Year ended March 31, 2026").duration_months == 12
