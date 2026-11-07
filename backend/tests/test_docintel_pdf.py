"""Phase 5: reading real PDF bytes with PyMuPDF (skipped if PyMuPDF is not installed)."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.docintel.pdf import read_pdf
from app.docintel.pipeline import extract_document
from app.models import Company, ExtractionStatus, FinancialFact, PeriodType
from app.seeds.loader import seed_reference_data, seed_test_universe
from tests.test_docintel import _document

pymupdf = pytest.importorskip("pymupdf")


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


def _make_pdf(path, lines, *, password=None):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    for x, y, text in lines:
        page.insert_text((x, y), text, fontsize=8, fontname="helv")
    kwargs = {}
    if password:
        kwargs = {
            "encryption": pymupdf.PDF_ENCRYPT_AES_256,
            "user_pw": password,
            "owner_pw": password,
        }
    doc.save(path, **kwargs)
    doc.close()


def test_read_pdf_returns_words_with_positions(tmp_path):
    path = tmp_path / "t.pdf"
    _make_pdf(path, [(60, 100, "Revenue from operations"), (350, 100, "1,000.00")])
    content = read_pdf(path, use_ocr=False)
    assert content.error is None and content.page_count == 1
    page = content.pages[0]
    assert not page.needs_ocr and page.method == "text_layer"
    assert [w.text for w in page.words] == ["Revenue", "from", "operations", "1,000.00"]
    assert page.words[-1].x0 >= 349


def test_end_to_end_from_a_generated_pdf(session, company, tmp_path):
    lines = [(200, 25, "Reliance Industries Limited")]
    lines.append((60, 40, "Statement of Standalone Audited Financial Results"))
    lines.append((470, 55, "(Rs. in crore)"))
    lines += [(300, 70, "Quarter ended"), (420, 70, "Year ended")]
    lines += [(300, 82, "30.09.2026"), (420, 82, "31.03.2026")]
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
    data = tmp_path / "data"
    (data / "documents").mkdir(parents=True)
    _make_pdf(data / "documents" / "x.pdf", lines)
    doc = _document(session, company, sha="j")
    doc.storage_path = "documents/x.pdf"
    run = extract_document(session, doc, data).extraction
    assert run.status is ExtractionStatus.COMPLETED, run.issues
    promoted = session.scalars(
        select(FinancialFact).where(
            FinancialFact.document_id == doc.id, FinancialFact.line_item == "net_profit"
        )
    ).all()
    assert {(f.period_type, f.original_value) for f in promoted} == {
        (PeriodType.QUARTERLY, Decimal("80.00")),
        (PeriodType.ANNUAL, Decimal("370.00")),
    }


def test_broken_and_password_protected_pdfs_are_reported(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 this is not really a pdf")
    assert read_pdf(bad).error is not None
    locked = tmp_path / "locked.pdf"
    _make_pdf(locked, [(60, 100, "secret")], password=str(uuid.uuid4()))
    assert read_pdf(locked).error == "password-protected PDF"
