"""Upload repairs found by replaying earlier real failures:

* an annual report rejected because a subsidiary was named on a contents page;
* results whose basis (consolidated/standalone) or unit the document never prints;
* shareholding patterns reported as a failed financial statement;
* scans with an unreliable text layer reported with a useless message.
"""

# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.docintel import inbox
from app.docintel.pdf import text_layer_quality
from app.docintel.statements import apply_overrides
from app.models import FinancialFact
from app.research import vault
from tests.test_research_vault import (  # noqa: F401  (fixtures + builders)
    data_dir,
    make_pdf,
    pymupdf,
    reliance,
    upload,
)


def plain_results_pdf(*, unit: bool, basis: bool) -> bytes:
    """A results table whose heading names neither the basis nor (optionally) the unit."""
    lines = [(200, 25, "Reliance Industries Limited")]
    title = (
        "Statement of Consolidated Audited Financial Results"
        if basis
        else "Audited Financial Results"
    )
    lines += [(60, 40, title)]
    if unit:
        lines += [(470, 55, "(Rs. in crore)")]
    lines += [
        (300, 70, "Quarter ended"),
        (420, 70, "Year ended"),
        (300, 82, "30.09.2026"),
        (420, 82, "31.03.2026"),
    ]
    y = 100
    for label, q, a in [
        ("Revenue from operations", "500.00", "1,900.00"),
        ("Other income", "10.00", "100.00"),
        ("Total income", "510.00", "2,000.00"),
        ("Total expenses", "400.00", "1,500.00"),
        ("Profit before tax", "110.00", "500.00"),
        ("Tax expense", "30.00", "130.00"),
        ("Net profit for the period", "80.00", "370.00"),
    ]:
        lines += [(60, y, label), (300, y, q), (420, y, a)]
        y += 14
    return make_pdf(lines)


# ---- basis and unit the document does not print ---------------------------------------------------


def test_a_missing_unit_or_basis_is_asked_once_and_the_same_file_is_then_accepted(
    session, reliance, data_dir
):
    entry = upload(
        session, reliance, "results.pdf", plain_results_pdf(unit=False, basis=False), data_dir
    )
    assert entry.status == vault.NEEDS_CLARIFICATION  # not REJECTED: the file is fine
    codes = {a["code"] for a in entry.ambiguities}
    assert codes == {"BASIS_UNSTATED", "UNIT_UNSTATED"}
    assert not session.scalar(select(func.count()).select_from(FinancialFact))  # nothing guessed

    vault.clarify(session, entry.id, {"basis": "CONSOLIDATED", "unit": "crore"}, data_dir=data_dir)
    session.refresh(entry)
    assert entry.status == vault.ACCEPTED, (entry.reject_reason, entry.ambiguities)
    fact = session.scalars(
        select(FinancialFact).where(FinancialFact.line_item == "revenue_from_operations")
    ).first()
    assert fact is not None and fact.basis.value == "CONSOLIDATED" and fact.original_unit == "crore"


def test_only_the_missing_value_is_asked(session, reliance, data_dir):
    entry = upload(
        session, reliance, "results.pdf", plain_results_pdf(unit=True, basis=False), data_dir
    )
    assert entry.status == vault.NEEDS_CLARIFICATION
    assert [a["code"] for a in entry.ambiguities] == ["BASIS_UNSTATED"]


def test_a_printed_basis_and_unit_are_never_overridden_by_the_uploader():
    printed = SimpleNamespace(basis="STANDALONE", unit="lakh", inherited=[], issues=[])
    blank = SimpleNamespace(
        basis=None,
        unit=None,
        inherited=[],
        issues=[{"code": "BASIS_UNKNOWN"}, {"code": "UNIT_UNKNOWN"}],
    )
    apply_overrides([printed, blank], {"basis": "CONSOLIDATED", "unit": "crore"})
    assert (printed.basis, printed.unit, printed.inherited) == ("STANDALONE", "lakh", [])
    assert (blank.basis, blank.unit) == ("CONSOLIDATED", "crore") and blank.issues == []
    assert blank.inherited == [
        "basis stated by the uploader",
        "unit stated by the uploader",
    ]  # provenance
    nothing = SimpleNamespace(basis=None, unit=None, inherited=[], issues=[])
    apply_overrides(
        [nothing], {"basis": "BOTH", "unit": "bananas"}
    )  # nonsense answers fill nothing
    assert (nothing.basis, nothing.unit) == (None, None)


def test_invalid_answers_are_refused(session, reliance, data_dir):
    entry = upload(
        session, reliance, "results.pdf", plain_results_pdf(unit=False, basis=True), data_dir
    )
    with pytest.raises(ValueError, match="unit must be"):
        vault.clarify(session, entry.id, {"unit": "bananas"}, data_dir=data_dir)


# ---- classification: shareholding patterns --------------------------------------------------------


def shareholding_pdf() -> bytes:
    return make_pdf(
        [
            (60, 40, "Reliance Industries Limited"),
            (60, 60, "Shareholding Pattern as on 30.09.2026"),
            (60, 80, "Promoter and promoter group 50.0"),
        ]
    )


def test_a_shareholding_pattern_is_classified_from_its_content_not_called_a_result():
    pages = ["Reliance Industries Limited\nShareholding Pattern as on 30.09.2026\nPromoter 50.0"]
    assert inbox.guess_doc_type("whatever.pdf", pages, 16) == "shareholding_pattern"
    assert (
        inbox.guess_doc_type("SHP_30092026.pdf", [""], 16) == "shareholding_pattern"
    )  # name as a last resort
    assert (
        inbox.guess_doc_type(
            "Q2 results.pdf", ["Quarter ended 30.09.2026 unaudited financial results"], 6
        )
        == "quarterly_result"
    )


def test_a_document_with_no_tables_is_accepted_as_reading_material_not_called_a_failed_statement(
    session, reliance, data_dir
):
    entry = upload(session, reliance, "SHP_30092026.pdf", shareholding_pdf(), data_dir)
    assert entry.status == vault.ACCEPTED and entry.category == "shareholding_pattern"
    assert entry.validation_status == "NO_FIGURES"
    assert any("no financial tables were read" in w for w in entry.detected["warnings"])
    assert not entry.reject_reason


def test_a_results_filing_with_no_tables_is_still_a_failure_with_advice(
    session, reliance, data_dir
):
    body = make_pdf(
        [
            (60, 40, "Reliance Industries Limited"),
            (60, 60, "Unaudited financial results for the quarter ended 30.09.2026"),
            (60, 80, "see attached"),
        ]
    )
    entry = upload(session, reliance, "results.pdf", body, data_dir)
    assert entry.status == vault.REJECTED
    assert "Excel/XBRL" in entry.reject_reason


# ---- scans with a poor text layer -----------------------------------------------------------------


def _scan_with_garbage_text() -> bytes:
    doc = pymupdf.open()
    for _ in range(3):
        page = doc.new_page(width=595, height=842)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
        pix.set_rect(pix.irect, (210, 210, 210))
        page.insert_image(pymupdf.Rect(20, 20, 300, 300), pixmap=pix)
        page.insert_text(
            (60, 400),
            "Quaner ended llJnauditedl lUnaudltedl lUneuditedl rUnauditedl Sltlernent",
            fontsize=9,
        )
    data = doc.tobytes()
    doc.close()
    return data


def test_a_scan_with_an_unreliable_text_layer_is_explained_with_a_way_forward(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(_scan_with_garbage_text())
    q = text_layer_quality(path)
    assert q["pages_with_images"] == 3 and q["malformed_share"] >= 0.04
    message = vault._no_statements_reason(path)
    assert "scan" in message and "unreliable" in message and "Excel/XBRL" in message

    clean = tmp_path / "clean.pdf"
    clean.write_bytes(
        make_pdf(
            [
                (
                    60,
                    60,
                    "Total income for the quarter ended and net profit before tax from operations",
                )
            ]
        )
    )
    assert "scan" not in vault._no_statements_reason(clean)


# ---- a long report whose contents page names a subsidiary -----------------------------------------


def long_report(*, subject: str, other: str, subject_pages: int, other_pages: int) -> bytes:
    doc = pymupdf.open()
    first = doc.new_page(width=595, height=842)
    first.insert_text((60, 60), f"Contents Board And Committee {other}", fontsize=10)
    for i in range(subject_pages + other_pages):
        page = doc.new_page(width=595, height=842)
        name = subject if i < subject_pages else other
        page.insert_text(
            (60, 80),
            f"{name} reported steady growth. {name} remains committed to shareholders.",
            fontsize=9,
        )
    data = doc.tobytes()
    doc.close()
    return data


def test_a_long_report_dominated_by_the_company_is_accepted_despite_a_subsidiary_on_the_first_page(
    session, reliance, data_dir
):
    body = long_report(
        subject="Reliance Industries Limited",
        other="Reliance Retail Limited",
        subject_pages=85,
        other_pages=1,
    )
    entry = vault.receive_upload(
        session,
        reliance,
        owner="alice",
        filename="annual-report.pdf",
        body=body,
        licence=vault.PUBLIC_FILING,
        data_dir=data_dir,
    )
    assert entry.status == vault.RECEIVED, entry.reject_reason
    assert entry.issuer_status == "CONFIRMED"


def test_a_long_report_that_is_really_the_subsidiarys_is_still_refused(session, reliance, data_dir):
    body = long_report(
        subject="Reliance Retail Limited",
        other="Reliance Retail Limited",
        subject_pages=85,
        other_pages=0,
    )
    entry = vault.receive_upload(
        session,
        reliance,
        owner="alice",
        filename="annual-report.pdf",
        body=body,
        licence=vault.PUBLIC_FILING,
        data_dir=data_dir,
    )
    assert entry.status == vault.REJECTED and "not by" in entry.reject_reason


# ---- uploaded reports reach the research sections --------------------------------------------------


def _report_doc(session, company, name, fy, pages, *, private=False):
    from datetime import UTC, datetime

    from app.models import Document, DocumentExtraction, DocumentPage, ExtractionStatus, VaultEntry

    doc = Document(
        company_id=company.id,
        url=f"user-upload://{name}",
        title=name,
        doc_type="annual_report",
        content_type="application/pdf",
        sha256=(name * 64)[:64],
        size_bytes=1,
        storage_path=f"documents/x/{name}.pdf",
        financial_period=fy,
        retrieved_at=datetime.now(UTC),
    )
    session.add(doc)
    session.flush()
    ext = DocumentExtraction(
        document_id=doc.id, extractor_version="t", status=ExtractionStatus.COMPLETED, issues=[]
    )
    session.add(ext)
    session.flush()
    for n in range(1, pages + 1):
        session.add(
            DocumentPage(
                extraction_id=ext.id, page_number=n, char_count=5, text="text", text_method="TEXT"
            )
        )
    if private:
        session.add(
            VaultEntry(
                company_id=company.id,
                document_id=doc.id,
                uploaded_by="alice",
                original_filename=f"{name}.pdf",
                sha256=doc.sha256,
                size_bytes=1,
                status="HELD_PRIVATE",
                visibility="PRIVATE",
                licence_class="USER_LICENSED",
            )
        )
    session.flush()
    return doc


def test_the_newest_annual_report_feeds_the_profile_not_the_longest(session, reliance):
    from app.research import profile

    old = _report_doc(session, reliance, "a", "FY2024", 300)
    new = _report_doc(session, reliance, "b", "FY2026", 200)
    doc, _, _ = profile._latest_annual_report(session, reliance.id)
    assert doc.id == new.id and doc.id != old.id


def test_a_privately_held_upload_never_feeds_a_report(session, reliance):
    from app.research import profile

    _report_doc(session, reliance, "pub", "FY2025", 100)
    private = _report_doc(session, reliance, "priv", "FY2026", 400, private=True)
    assert private.id in profile.private_document_ids(session, reliance.id)
    doc, _, _ = profile._latest_annual_report(session, reliance.id)
    assert doc.title == "pub"
