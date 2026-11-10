"""Uploads: format checks, duplicates, issuer, extraction, conflicts, privacy (Phase 11)."""

import io
import zipfile
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Company, Document, EvidenceRecord, VaultEntry
from app.research import vault
from app.seeds.loader import seed_reference_data, seed_test_universe

pymupdf = pytest.importorskip("pymupdf")
openpyxl = pytest.importorskip("openpyxl")

RELIANCE_ISIN = "INE002A01018"


# ---- builders -------------------------------------------------------------------------------------


def make_pdf(lines, *, pages=1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=595, height=842)
        for x, y, text in lines:
            page.insert_text((x, y), text, fontsize=8, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def results_pdf(
    issuer="Reliance Industries Limited",
    revenue="1,900.00",
    quarter="500.00",
    q_date="30.09.2026",
    a_date="31.03.2026",
    total="2,000.00",
    expenses="1,500.00",
):
    lines = [(200, 25, issuer)] if issuer else []
    lines += [
        (60, 40, "Statement of Consolidated Audited Financial Results"),
        (470, 55, "(Rs. in crore)"),
        (300, 70, "Quarter ended"),
        (420, 70, "Year ended"),
        (300, 82, q_date),
        (420, 82, a_date),
    ]
    y = 100
    for label, q, a in [
        ("Revenue from operations", quarter, revenue),
        ("Other income", "10.00", "100.00"),
        ("Total income", "510.00", total),
        ("Total expenses", "400.00", expenses),
        ("Profit before tax", "110.00", "500.00"),
        ("Tax expense", "30.00", "130.00"),
        ("Net profit for the period", "80.00", "370.00"),
    ]:
        lines += [(60, y, label), (300, y, q), (420, y, a)]
        y += 14
    return make_pdf(lines)


def scanned_pdf() -> bytes:
    """A page that is only a picture: no text layer."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 200), False)
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(pymupdf.Rect(50, 50, 400, 400), pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def csv_bytes(isin=RELIANCE_ISIN, value="1900"):
    return (
        "isin,statement,line_item,period_end,period_type,basis,value,unit\n"
        f"{isin},INCOME,revenue_from_operations,2026-03-31,ANNUAL,CONSOLIDATED,{value},crore\n"
        f"{isin},INCOME,net_profit,2026-03-31,ANNUAL,CONSOLIDATED,370,crore\n"
    ).encode()


def xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def reliance(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "documents").mkdir()
    return tmp_path


def upload(session, company, name, body, data_dir, **kw):
    kw.setdefault("licence", vault.PUBLIC_FILING)
    entry = vault.receive_upload(
        session, company, owner="alice", filename=name, body=body, data_dir=data_dir, **kw
    )
    if entry.status == vault.RECEIVED:
        vault.process_entry(session, entry.id, data_dir=data_dir)
    return entry


# ---- stage 1: what the file really is -------------------------------------------------------------


def test_a_real_pdf_is_recognised_from_its_content():
    info = vault.inspect_upload("results.pdf", results_pdf())
    assert info.kind == "pdf" and info.pages == 1 and info.mime == "application/pdf"


def test_the_extension_is_never_trusted():
    exe = b"MZ\x90\x00" + b"\x00" * 200
    assert vault.inspect_upload("results.pdf", exe).kind is None
    html = b"<html><script>alert(1)</script></html>"
    assert vault.inspect_upload("results.pdf", html).kind is None
    pdf_named_xlsx = vault.inspect_upload("results.xlsx", results_pdf())
    assert pdf_named_xlsx.kind is None and "content is PDF" in pdf_named_xlsx.problem
    assert vault.inspect_upload("results.pdf", csv_bytes()).kind is None


def test_empty_corrupt_and_oversized_files_are_refused(monkeypatch):
    assert "empty" in vault.inspect_upload("a.pdf", b"").problem
    corrupt = vault.inspect_upload("a.pdf", b"%PDF-1.7\nthis is not really a pdf")
    assert corrupt.kind is None and "corrupt" in corrupt.problem
    monkeypatch.setattr(get_settings(), "research_max_upload_mb", 1)
    big = b"%PDF-1.7\n" + b"0" * (1024 * 1024 + 10)
    assert "larger than" in vault.inspect_upload("a.pdf", big).problem


def test_encrypted_pdfs_are_refused_rather_than_half_read():
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "secret", fontsize=8)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    assert vault.inspect_upload("a.pdf", data).kind is None


def test_spreadsheets_are_checked_for_structure_and_macros():
    ok = xlsx_bytes([["isin", "statement"]])
    assert vault.inspect_upload("a.xlsx", ok).kind == "xlsx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    assert "ZIP archives are not accepted" in vault.inspect_upload("a.zip", buf.getvalue()).problem
    macro = io.BytesIO()
    with zipfile.ZipFile(macro, "w") as z:
        z.writestr("[Content_Types].xml", "<x/>")
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/vbaProject.bin", "x")
    assert "macros" in vault.inspect_upload("a.xlsx", macro.getvalue()).problem
    assert "corrupt" in vault.inspect_upload("a.xlsx", b"PK\x03\x04nonsense").problem


def test_a_csv_must_be_in_the_financials_format():
    assert vault.inspect_upload("a.csv", csv_bytes()).kind == "csv"
    other = vault.inspect_upload("a.csv", b"name,age\nbob,3\n")
    assert other.kind is None and "missing column" in other.problem


def test_fiscal_labels():
    from datetime import date

    assert vault.fiscal_labels(date(2026, 3, 31), 12) == ("FY2026", None)
    assert vault.fiscal_labels(date(2026, 9, 30), 3) == ("FY2027", "Q2")
    assert vault.fiscal_labels(date(2026, 9, 30), 6) == ("FY2027", "H1")
    assert vault.fiscal_labels(date(2026, 12, 31), 9) == ("FY2027", "9M")
    assert vault.fiscal_labels(None, None) == (None, None)


# ---- stages 2-7 against the database --------------------------------------------------------------


def test_a_valid_filing_is_accepted_organised_and_updates_the_company(session, reliance, data_dir):
    entry = upload(session, reliance, "q2.pdf", results_pdf(), data_dir)
    assert entry.status == vault.ACCEPTED, (entry.reject_reason, entry.detected)
    assert entry.issuer_status == "CONFIRMED" and entry.validation_status in ("PASSED", "PARTIAL")
    assert entry.visibility == "SHARED" and entry.category == "quarterly_result"
    assert entry.fiscal_year == "FY2027" and entry.quarter in ("Q2", None)
    assert entry.basis == "CONSOLIDATED" and entry.sha256 and entry.size_bytes
    assert entry.evidence_refs[0]["count"] > 0
    assert {"revenue", "net_profit"} <= set(entry.resolved_requirements)
    doc = session.get(Document, entry.document_id)
    assert doc.company_id == reliance.id and doc.association_status == "CONFIRMED"
    refs = session.scalars(select(EvidenceRecord).where(EvidenceRecord.document_id == doc.id)).all()
    assert refs and all(r.source_page for r in refs)  # page-level evidence is preserved


def test_the_same_file_is_never_processed_twice(session, reliance, data_dir):
    body = results_pdf()
    first = upload(session, reliance, "q2.pdf", body, data_dir)
    again = upload(session, reliance, "copy of q2.pdf", body, data_dir)
    assert again.status == vault.DUPLICATE and again.supersedes_id == first.id
    assert again.document_id == first.document_id and "already uploaded" in again.reject_reason
    assert session.scalar(select(EvidenceRecord.id).limit(1)) is not None
    assert len(session.scalars(select(Document).where(Document.sha256 == first.sha256)).all()) == 1


def test_a_document_from_another_issuer_is_refused(session, reliance, data_dir):
    body = results_pdf(issuer="Beta Chemicals Limited")
    entry = upload(session, reliance, "beta.pdf", body, data_dir)
    assert entry.status == vault.REJECTED and "issued by" in entry.reject_reason
    assert entry.document_id is None


def test_an_unreadable_issuer_needs_the_users_confirmation_before_anything_is_read(
    session, reliance, data_dir
):
    entry = upload(session, reliance, "anon.pdf", results_pdf(issuer=None), data_dir)
    assert entry.status == vault.NEEDS_CLARIFICATION
    assert entry.ambiguities[0]["code"] == "ISSUER_UNCONFIRMED"
    assert not session.scalars(
        select(EvidenceRecord).where(EvidenceRecord.document_id == entry.document_id)
    ).all()  # nothing was read from it yet
    done = vault.clarify(session, entry.id, {"confirm_issuer": True}, data_dir=data_dir)
    # The user's confirmation links the document, but a statement page that names nobody still
    # cannot prove the figures are this company's: the extractor refuses them, and says why.
    assert done.issuer_status == "FORCED"
    assert session.get(Document, done.document_id).association_status == "FORCED"
    assert done.status == vault.REJECTED and "entity not confirmed" in done.reject_reason


def test_clarifying_something_that_needs_no_clarification_is_refused(session, reliance, data_dir):
    entry = upload(session, reliance, "q2.pdf", results_pdf(), data_dir)
    with pytest.raises(ValueError, match="does not need clarification"):
        vault.clarify(session, entry.id, {"confirm_issuer": True})


def test_a_scanned_pdf_without_ocr_is_reported_not_guessed(session, reliance, data_dir):
    entry = upload(session, reliance, "scan.pdf", scanned_pdf(), data_dir)
    assert entry.status in (vault.REJECTED, vault.NEEDS_CLARIFICATION)
    if entry.status == vault.REJECTED:
        assert "scans" in entry.reject_reason or "no financial" in entry.reject_reason
    assert not entry.resolved_requirements


def test_a_pdf_that_is_not_a_financial_document_yields_no_figures(session, reliance, data_dir):
    body = make_pdf(
        [(60, 60, "Reliance Industries Limited"), (60, 90, "Lovely letter to shareholders")]
    )
    entry = upload(session, reliance, "letter.pdf", body, data_dir)
    assert entry.status in (vault.REJECTED, vault.NEEDS_CLARIFICATION)
    assert not entry.resolved_requirements


def test_an_old_period_is_flagged_and_does_not_settle_current_requirements(
    session, reliance, data_dir
):
    body = results_pdf(q_date="30.09.2019", a_date="31.03.2020")
    entry = upload(session, reliance, "old.pdf", body, data_dir)
    assert entry.status == vault.ACCEPTED
    assert any("too old" in w for w in entry.detected["warnings"])
    assert not entry.resolved_requirements  # stale figures settle nothing
    from app.research import readiness as rd

    assert rd.evaluate(session, reliance).by_id("net_profit").status != rd.AVAILABLE


def test_conflicting_values_are_listed_and_never_hidden(session, reliance, data_dir):
    first = upload(session, reliance, "a.pdf", results_pdf(revenue="1,900.00"), data_dir)
    assert first.status == vault.ACCEPTED
    second = upload(
        session,
        reliance,
        "b.pdf",
        results_pdf(revenue="2,400.00", total="2,500.00", expenses="2,000.00"),
        data_dir,
    )
    assert second.status in (vault.ACCEPTED, vault.REJECTED)
    if second.status == vault.ACCEPTED:
        assert second.conflicts, "a 26% disagreement must be shown"
        assert second.validation_status == "PARTIAL"
        c = second.conflicts[0]
        assert c["selected"] in ("THIS_UPLOAD", "EXISTING")
        assert c["note"] and ("differs from" in c["note"] or float(c["difference_pct"]) > 0.5)


def test_a_document_declared_private_is_stored_but_never_feeds_shared_evidence(
    session, reliance, data_dir
):
    entry = upload(
        session, reliance, "private.pdf", results_pdf(), data_dir, licence="USER_LICENSED"
    )
    assert entry.status == vault.HELD_PRIVATE and entry.visibility == "PRIVATE"
    assert not session.scalars(
        select(EvidenceRecord).where(EvidenceRecord.document_id == entry.document_id)
    ).all()
    assert not entry.resolved_requirements


# ---- tables ---------------------------------------------------------------------------------------


def test_a_csv_of_the_companys_own_figures_is_imported_at_lowest_authority(
    session, reliance, data_dir
):
    entry = vault.receive_upload(
        session, reliance, owner="alice", filename="fin.csv", body=csv_bytes(),
        licence=vault.PUBLIC_FILING, data_dir=data_dir,
    )  # fmt: skip
    entry = vault.process_entry(session, entry.id, data_dir=data_dir)
    assert entry.status == vault.NEEDS_CLARIFICATION
    assert entry.ambiguities[0]["code"] == "SOURCE_REFERENCE_REQUIRED"
    done = vault.clarify(
        session,
        entry.id,
        {"source_reference": "Q2 FY27 results, BSE, 30 Oct 2026"},
        data_dir=data_dir,
    )
    assert done.status == vault.ACCEPTED, (done.reject_reason, done.detected)
    assert done.issuer_status == "UNVERIFIED" and any(
        "lowest source authority" in w for w in done.detected["warnings"]
    )
    assert "revenue" in done.resolved_requirements


def test_a_table_with_another_companys_rows_is_refused_whole(session, reliance, data_dir):
    entry = upload(session, reliance, "other.csv", csv_bytes(isin="INE467B01029"), data_dir)
    assert entry.status == vault.REJECTED and "not Reliance" in entry.reject_reason


def test_a_table_with_one_bad_row_imports_nothing(session, reliance, data_dir):
    body = (
        csv_bytes()
        + f"{RELIANCE_ISIN},INCOME,revenue_from_operations,2025-03-31,ANNUAL,CONSOLIDATED,,crore\n".encode()
    )
    entry = vault.receive_upload(
        session, reliance, owner="alice", filename="bad.csv", body=body,
        licence=vault.PUBLIC_FILING, data_dir=data_dir,
    )  # fmt: skip
    entry.clarification = {"source_reference": "x"}
    entry = vault.process_entry(session, entry.id, data_dir=data_dir)
    assert entry.status == vault.REJECTED and "failed validation" in entry.reject_reason


def test_xlsx_in_the_same_format_works_like_csv(session, reliance, data_dir):
    rows = [
        ["isin", "statement", "line_item", "period_end", "period_type", "basis", "value", "unit"],
        [
            RELIANCE_ISIN,
            "INCOME",
            "revenue_from_operations",
            "2026-03-31",
            "ANNUAL",
            "CONSOLIDATED",
            1900,
            "crore",
        ],
    ]
    entry = vault.receive_upload(
        session, reliance, owner="alice", filename="fin.xlsx", body=xlsx_bytes(rows),
        licence=vault.PUBLIC_FILING, data_dir=data_dir,
    )  # fmt: skip
    entry.clarification = {"source_reference": "annual report FY26"}
    entry = vault.process_entry(session, entry.id, data_dir=data_dir)
    assert entry.status == vault.ACCEPTED and entry.evidence_refs[0]["count"] > 0


# ---- access ---------------------------------------------------------------------------------------


def test_other_users_see_validated_public_filings_but_not_your_private_material(
    session, reliance, data_dir
):
    shared = upload(session, reliance, "q2.pdf", results_pdf(), data_dir)
    private = upload(
        session, reliance, "mine.pdf", results_pdf(revenue="2,222.00"), data_dir,
        licence="USER_LICENSED",
    )  # fmt: skip
    assert shared.status == vault.ACCEPTED and private.status == vault.HELD_PRIVATE
    bob = {e.id for e in vault.list_vault(session, reliance.id, "bob")}
    assert shared.id in bob and private.id not in bob
    out = vault.entry_out(shared, "bob")
    assert out["filename"] is None and out["uploaded_by"] == "another user" and out["steps"] == []
    assert out["fiscal_year"] and out["validation_status"]  # the useful metadata is shared
    alice = {e.id for e in vault.list_vault(session, reliance.id, "alice")}
    assert {shared.id, private.id} <= alice
    assert vault.can_view(private, "admin-user", is_admin=True)
    assert session.scalar(select(VaultEntry.id).where(VaultEntry.id == private.id)) is not None


def test_a_stored_document_is_reused_by_the_next_researcher(session, reliance, data_dir):
    upload(session, reliance, "q2.pdf", results_pdf(), data_dir)
    from app.research import readiness as rd

    ready = rd.evaluate(session, reliance)
    assert ready.by_id("net_profit").ok  # nobody has to upload it again


def test_unknown_line_items_in_a_table_are_rejected_with_the_valid_names(
    session, reliance, data_dir
):
    body = csv_bytes().replace(b"revenue_from_operations", b"Sales Turnover")
    entry = upload(session, reliance, "names.csv", body, data_dir)
    assert entry.status == vault.REJECTED and "Sales Turnover" in entry.reject_reason
    assert "revenue_from_operations" in entry.reject_reason


def test_the_table_template_lists_the_accepted_line_items():
    t = vault.template()
    assert "revenue_from_operations" in t["line_items"]["INCOME"]
    assert t["columns"] and "missing is not zero" in " ".join(t["rules"])


def test_values_the_extractor_refused_are_reported_not_hidden(session, reliance, data_dir):
    # revenue changed but total income not: the arithmetic check fails, so those cells are refused
    entry = upload(session, reliance, "bad.pdf", results_pdf(revenue="2,400.00"), data_dir)
    assert entry.status == vault.ACCEPTED  # the quarterly column is internally consistent
    refused = entry.detected["not_accepted"]
    assert refused["count"] > 0 and "arithmetic check" in refused["reasons"][0]
    assert any("not accepted" in w for w in entry.detected["warnings"])


# ---- key figures from releases, period of a document, re-processing -------------------------------


def release_pdf(issuer="Reliance Industries Limited"):
    lines = [(200, 25, issuer)] if issuer else []
    lines += [(60, 45, "Press Release Q2FY27 Results"), (60, 58, "Rs. in Crores")]
    x = (200, 260, 320)
    lines += [
        (60, 80, "Ratios"),
        *[(x[i], 80, h) for i, h in enumerate(("Q2FY26", "Q1FY27", "Q2FY27"))],
    ]
    for n, (label, vals) in enumerate(
        [
            ("GNPA", ("1.73", "1.47", "1.39")),
            ("NNPA", ("0.42", "0.38", "0.36")),
            ("CRAR", ("14.62", "15.67", "16.09")),
        ],
        start=1,
    ):
        lines += [(60, 80 + 14 * n, label), *[(x[i], 80 + 14 * n, v) for i, v in enumerate(vals)]]
    return make_pdf(lines)


def test_key_figures_from_a_release_are_accepted_with_page_evidence(session, reliance, data_dir):
    entry = upload(session, reliance, "release.pdf", release_pdf(), data_dir)
    assert entry.status == vault.ACCEPTED, (entry.reject_reason, entry.detected)
    rows = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.metric.like("RATIOS.%"), EvidenceRecord.document_id == entry.document_id
        )
    ).all()
    q2 = {r.metric: r for r in rows if r.period_end == date(2026, 9, 30)}
    assert q2["RATIOS.gross_npa_ratio"].value == D("1.39")
    assert q2["RATIOS.net_npa_ratio"].value == D("0.36")
    assert q2["RATIOS.capital_adequacy_ratio"].value == D("16.09")
    assert all(r.source_page == 1 and r.unit == "percent" and r.basis is None for r in rows)
    assert entry.fiscal_year == "FY2027" and entry.quarter == "Q2"


def test_a_release_that_never_names_the_company_needs_the_uploaders_confirmation(
    session, reliance, data_dir
):
    entry = upload(session, reliance, "nameless.pdf", release_pdf(issuer=None), data_dir)
    assert entry.status == vault.NEEDS_CLARIFICATION
    assert not session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.metric.like("RATIOS.%"), EvidenceRecord.document_id == entry.document_id
        )
    ).all()
    done = vault.clarify(session, entry.id, {"confirm_issuer": True}, data_dir=data_dir)
    assert done.status == vault.ACCEPTED and done.issuer_status == "FORCED"
    rec = session.scalars(
        select(EvidenceRecord).where(
            EvidenceRecord.metric == "RATIOS.gross_npa_ratio",
            EvidenceRecord.document_id == done.document_id,
        )
    ).first()
    assert rec is not None  # the figures count, labelled as confirmed by the uploader
    from app.models import ExtractedFact

    notes = session.scalars(
        select(ExtractedFact.validation_notes).where(
            ExtractedFact.document_id == done.document_id,
            ExtractedFact.canonical_item == "gross_npa_ratio",
        )
    ).first()
    assert any("key figure" in n for n in notes)


def test_the_period_of_a_document_ignores_unreadable_and_future_columns():
    cols = [
        {"period_end": "2026-09-30", "duration_months": 3},
        {"period_end": "2026-09-30", "duration_months": 6},
        {"period_end": "2026-03-31", "duration_months": 12},
        {"period_end": "2029-06-30", "duration_months": 3},  # a misread year
        {"period_end": "2026-12-31", "duration_months": 3, "problem": "no date in header"},
        {"period_end": "2026-09-30", "duration_months": 3, "is_note": True},
    ]
    assert vault.document_period(cols, date(2026, 11, 10)) == (date(2026, 9, 30), 3)  # the quarter
    assert vault.document_period(
        [{"period_end": "2026-09-30", "duration_months": None}], date(2026, 11, 10)
    ) == (date(2026, 9, 30), None)
    assert vault.document_period([], date(2026, 11, 10)) is None


def test_an_upload_can_be_processed_again_without_uploading_it_again(session, reliance, data_dir):
    first = upload(session, reliance, "scan.pdf", scanned_pdf(), data_dir)
    assert first.status in (vault.REJECTED, vault.NEEDS_CLARIFICATION)
    again = vault.reprocess(session, first.id, data_dir=data_dir)
    assert again.id == first.id and again.status in (vault.REJECTED, vault.NEEDS_CLARIFICATION)
    body = results_pdf()
    dup = upload(session, reliance, "copy.pdf", body, data_dir)
    twin = upload(session, reliance, "copy2.pdf", body, data_dir)
    assert twin.status == vault.DUPLICATE
    with pytest.raises(ValueError, match="nothing stored"):
        vault.reprocess(session, twin.id, data_dir=data_dir)
    assert dup.status == vault.ACCEPTED
