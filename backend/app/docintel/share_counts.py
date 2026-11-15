"""Issued, subscribed and paid-up share counts printed in the share-capital note.

An annual report prints the number of equity shares in issue next to the face value and the paid-up
amount ("Issued, subscribed and paid-up capital 15,39,33,68,328 (31 March, 2025: 7,65,22,21,674)
Equity Shares of ` 1/- each fully paid-up ... 15,393,368 ..."). The count is trusted only when it
closes an identity with the printed amount:

    count x face value  =  printed paid-up amount   (in the statement's unit, +/- rounding)

A misread digit cannot satisfy that by accident, so the check stands in for the arithmetic identity
that single-line balance-sheet items lack. Authorised capital, issued-but-not-subscribed lines and
subsidiaries' holdings are skipped: only a line that says "paid-up" and is not the authorised
capital is read. The stored figure is the count AS PRINTED at the balance-sheet date, so a later
split or bonus is applied by the corporate-action adjustment, never here.

Stored as a SHAREHOLDING disclosure `total` in unit `shares`, i.e. the evidence metric
`SHAREHOLDING.total.shares`, which market capitalisation and the DCF read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentExtraction,
    ExtractedFact,
    ExtractedStatement,
    ValidationStatus,
)

READER = "share-capital note: paid-up count x face value = printed capital"

_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}
_COUNT = r"\d{1,3}(?:,\d{2,3})+"
_SHARES_OF = r"(?:equity\s+)?shares?\s+of\s*[^\d\s]{0,3}\s*(?P<fv>\d+(?:\.\d+)?)\s*(?:/-)?\s*each"
_LINE = re.compile(
    rf"(?P<count>{_COUNT})\s*(?:\((?P<pd>[^():]{{4,40}}):\s*(?P<pcount>{_COUNT})\))?\s*{_SHARES_OF}",
    re.IGNORECASE,
)
_PREVIOUS = re.compile(rf"\(\s*previous\s+year[:\s]*(?P<pcount>{_COUNT})", re.IGNORECASE)
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_AS_AT = re.compile(r"as\s+(?:at|on)\s+(?P<d>.{0,28})", re.IGNORECASE)
_D_DMY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s,.\-]+([A-Za-z]{3,9})[\s,.\-]+(\d{4})")
_D_MDY = re.compile(r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_D_NUM = re.compile(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})")
_UNITS = (
    (Decimal(1), "rupees"),
    (Decimal(10) ** 3, "thousand"),
    (Decimal(10) ** 5, "lakh"),
    (Decimal(10) ** 6, "million"),
    (Decimal(10) ** 7, "crore"),
)


@dataclass(frozen=True)
class ShareCount:
    page: int
    as_at: date
    count: int
    face_value: Decimal
    printed_count: str
    amount_printed: str
    unit: str
    method: str  # text layer or OCR of the page
    prior_count: int | None = None  # the count printed for the comparison date, as reported then


def _to_date(text: str) -> date | None:
    for rx, order in ((_D_DMY, "dmy"), (_D_MDY, "mdy"), (_D_NUM, "num")):
        m = rx.search(text)
        if not m:
            continue
        try:
            if order == "dmy":
                day, mon, year = int(m[1]), _MONTHS.get(m[2].lower()), int(m[3])
            elif order == "mdy":
                day, mon, year = int(m[2]), _MONTHS.get(m[1].lower()), int(m[3])
            else:
                day, mon, year = int(m[1]), int(m[2]), int(m[3])
            if mon:
                return date(year, mon, day)
        except ValueError:
            continue
    return None


def _int(raw: str) -> int:
    return int(raw.replace(",", ""))


def _amount_check(count: int, face: Decimal, following: str) -> tuple[str, str] | None:
    """The printed amount that equals count x face value in some unit: (printed text, unit name)."""
    total = Decimal(count) * face
    for number in _NUMBER.findall(following)[:6]:
        printed = Decimal(number.replace(",", ""))
        if printed == 0:
            continue
        decimals = len(number.split(".")[1]) if "." in number else 0
        tolerance = Decimal("0.5") * (Decimal(10) ** -decimals)
        for factor, name in _UNITS:
            if abs(total / factor - printed) <= tolerance:
                return number, name
    return None


def find_share_counts(pages: list[tuple[int, str, str]]) -> list[ShareCount]:
    """`pages` = (page number, text, text method). Counts whose identity with the capital holds."""
    found: list[ShareCount] = []
    for number, raw, method in pages:
        text = re.sub(r"\s+", " ", raw or "")
        for m in _LINE.finditer(text):
            label = text[max(0, m.start() - 140) : m.start()].lower()
            paid, authorised = label.rfind("paid"), label.rfind("authori")
            if paid < 0 or authorised > paid:
                continue
            if re.search(r"\(\s*previous\s+year[:\s]*$", label):
                continue  # the comparison figure of the line before, not a line of its own
            face = Decimal(m["fv"])
            count = _int(m["count"])
            check = _amount_check(count, face, text[m.end() : m.end() + 170])
            if check is None:
                continue
            heads = [
                d
                for a in _AS_AT_ALL(text[max(0, m.start() - 600) : m.start()])
                if (d := _to_date(a))
            ]
            if not heads:
                continue
            as_at = max(heads)
            prior = m["pcount"]
            if prior is None:
                tail = _PREVIOUS.search(text[m.end() : m.end() + 160])
                prior = tail["pcount"] if tail else None
            found.append(
                ShareCount(
                    page=number,
                    as_at=as_at,
                    count=count,
                    face_value=face,
                    printed_count=m["count"],
                    amount_printed=check[0],
                    unit=check[1],
                    method=method,
                    prior_count=_int(prior) if prior else None,
                )
            )
    # one figure per date: the same note is repeated for the standalone and consolidated pages
    unique: dict[tuple[date, int], ShareCount] = {}
    for f in found:
        unique.setdefault((f.as_at, f.count), f)
    return sorted(unique.values(), key=lambda f: (f.as_at, f.page))


def _AS_AT_ALL(text: str) -> list[str]:  # noqa: N802 - reads like the regex it wraps
    return [m["d"] for m in _AS_AT.finditer(text)]


def persist(
    session: Session,
    run: DocumentExtraction,
    document: Document,
    pages: list[tuple[int, str, str]],
    promote: bool = True,
) -> list[ShareCount]:
    """Replace this run's share-count rows with the ones the pages support."""
    session.execute(
        delete(ExtractedStatement).where(
            ExtractedStatement.extraction_id == run.id, ExtractedStatement.detection == READER
        )
    )
    counts = find_share_counts(pages)
    for c in counts:
        stmt = ExtractedStatement(
            extraction_id=run.id,
            page_number=c.page,
            statement="SHAREHOLDING",
            basis=None,
            unit="shares",
            title="Issued, subscribed and paid-up capital",
            detection=READER,
            columns=[{"header": f"As at {c.as_at.isoformat()}", "period_end": c.as_at.isoformat()}],
            rows_matched=1,
            rows_unmatched=0,
            checks=[
                {
                    "name": "count x face value = printed paid-up capital",
                    "form": f"{c.printed_count} x {c.face_value} = {c.amount_printed} ({c.unit})",
                    "result": "PASSED",
                }
            ],
        )
        session.add(stmt)
        session.flush()
        session.add(
            ExtractedFact(
                extraction_id=run.id,
                statement_id=stmt.id,
                document_id=document.id,
                company_id=document.company_id,
                statement="SHAREHOLDING",
                basis=None,
                canonical_item="total",
                raw_label=(
                    f"Issued, subscribed and paid-up equity shares of {c.face_value} each "
                    f"(as printed in the share-capital note, {c.method})"
                ),
                page_number=c.page,
                row_index=0,
                column_index=0,
                column_header=f"As at {c.as_at.isoformat()}",
                period_end=c.as_at,
                duration_months=None,
                audited=None,
                raw_value=c.printed_count[:60],
                original_value=Decimal(c.count),
                original_unit="shares",
                value_inr=None,
                bbox=None,
                dimension=None,
                validation_status=ValidationStatus.PASSED,
                validation_notes=[
                    f"{c.printed_count} shares x face value {c.face_value} equals the printed "
                    f"paid-up capital {c.amount_printed} ({c.unit})",
                    "count as printed at the date; a later split or bonus is not applied here",
                ],
                promoted=promote,
                not_promoted_reason=None if promote else "promotion disabled",
                needs_review=False,
                ocr_confidence=None,
            )
        )
    session.flush()
    return counts
