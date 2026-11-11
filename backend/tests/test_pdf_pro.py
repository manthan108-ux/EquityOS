"""The institutional PDF renderer: structure, evidence labelling, sector lens, graceful gaps."""

import copy
import json
from pathlib import Path

import pytest

from app.research import report as rp
from app.research.pdf_pro import render
from tests.research_helpers import complete_company
from tests.test_research_pdf import BANK, IT, client, make_report, pdf_text  # noqa: F401
from tests.test_sector import seeded  # noqa: F401

pymupdf = pytest.importorskip("pymupdf")
KW = {"version": 1, "report_id": "3134f0a2-479d-42f3-bd40-3e6ffbf50ee0", "content_hash": "a" * 64}
ALICE = {"X-API-Key": "alice-secret"}


def _item(req, label, value, unit, provider="DOCUMENT_EXTRACTION", ev=1):
    return {
        "requirement": req,
        "label": label,
        "importance": "CRITICAL",
        "value": value,
        "unit": unit,
        "period_end": "2026-03-31",
        "period_type": "ANNUAL",
        "basis": "CONSOLIDATED",
        "dimension": None,
        "period_note": None,
        "source": {
            "evidence_id": ev,
            "document_id": "abcdef12-0000",
            "page": 7,
            "provider": provider,
            "quality": "HIGH",
        },
    }


def _sec(key, title, items=(), gaps=(), **extra):
    return {
        "key": key,
        "title": title,
        "status": "COMPLETE",
        "items": list(items),
        "gaps": list(gaps),
        **extra,
    }


def _year(fy, value, ev):
    return {
        "unit": "inr",
        "basis": "CONSOLIDATED",
        "value": value,
        "fiscal_year": fy,
        "period_end": f"{fy[2:]}-03-31",
        "source": {"evidence_id": ev},
    }


def _report():
    """A small hand-built report content (no database): an IT-services company."""
    series = {
        "revenue": [_year("FY2024", "1000000000000", 1), _year("FY2025", "1100000000000", 2)],
        "net_profit": [_year("FY2024", "100000000000", 3), _year("FY2025", "90000000000", 4)],
        "total_assets": [],
        "total_equity": [],
    }
    return {
        "meta": {
            "company": "Demo IT Ltd",
            "mode": "COMPLETE",
            "as_of": "2026-11-11",
            "profile": "IT_SERVICES",
            "complete": True,
            "preferences": {"basis": "CONSOLIDATED", "period_label": "FY2025"},
        },
        "readiness": {
            "score": 80,
            "eligibility": "AUTOMATIC",
            "confidence": "MEDIUM",
            "components": {},
        },
        "sections": [
            _sec(
                "overview",
                "Company overview",
                company={
                    "legal_name": "Demo IT Ltd",
                    "sector": "it_services",
                    "industry": "it_services_and_consulting",
                    "symbols": ["DEMO"],
                    "profile": "IT services",
                },
            ),
            _sec(
                "financial_performance",
                "Financial performance",
                [
                    _item("revenue", "Revenue", "1100000000000", "inr", ev=2),
                    _item("net_profit", "Net profit", "90000000000", "inr", ev=4),
                    _item(
                        "operating_margin",
                        "Operating margin",
                        "24.5",
                        "percent",
                        provider="EVIDENCE_ENGINE",
                        ev=9,
                    ),
                ],
            ),
            _sec("historical", "Historical", series=series),
            _sec("peer_comparison", "Peers", peers={"peers": [], "available": False}),
            _sec("scorecard", "Scorecard", score={"available": False, "reason": "no peers"}),
        ],
        "limitations": {
            "missing_critical": [],
            "missing_important": [],
            "missing_optional": [],
            "unavailable_calculations": [],
            "limits_on_conclusions": [],
            "applied_price_adjustments": [],
            "price_history_notes": [],
        },
    }


def _flat(report, mode="COMPLETE"):
    return pdf_text(render(report, mode=mode, **KW))[0].replace("\n", " ")


def test_cover_and_executive_summary_keep_score_and_readiness_separate():
    text, pages = pdf_text(render(_report(), mode="COMPLETE", **KW))
    assert "EQUITY RESEARCH" in text and "Demo IT Ltd" in text and "DEMO" in text
    assert "FINANCIAL SCORE" in text and "RESEARCH READINESS" in text
    assert "1. Executive summary" in text and "Key observations" in text
    assert "separate measures" in text.replace("\n", " ")
    assert "None" not in text and pages >= 3


def test_every_statement_is_labelled_and_growth_is_derived_with_its_formula():
    flat = _flat(_report())
    assert "VERIFIED FACT" in flat and "DERIVED" in flat
    assert "grew 10.0%" in flat  # revenue 1.00 -> 1.10 lakh crore
    assert "fell 10.0%" in flat  # profit 100 -> 90: never "grew -10%"
    assert "(this year / previous year - 1) x 100" in flat


def test_no_recommendation_or_target_price_is_generated():
    low = _flat(_report()).lower()
    assert "no buy, sell or hold recommendation" in low
    assert "no target price is given" in low
    for word in ("strong buy", "outperform", "upside of"):
        assert word not in low


def test_unbuilt_analysis_is_declared_not_faked():
    flat = _flat(_report())
    assert "not part of this edition" in flat
    assert "scenarios" in flat and "investment thesis" in flat


def test_the_sector_lens_and_kpis_follow_the_profile():
    flat = _flat(_report())
    assert "IT-services company is read through" in flat and "Operating margin" in flat
    assert "free cash flow and cash conversion are not meaningful" not in flat  # bank-only note
    bank = _report()
    bank["meta"]["profile"] = "BANK"
    bflat = _flat(bank)
    assert "A bank is read through" in bflat
    assert "free cash flow and cash conversion are not meaningful" in bflat


def test_missing_data_degrades_without_invented_content():
    bare = _report()
    bare["sections"] = [s for s in bare["sections"] if s["key"] == "overview"]
    bare["sections"].append(_sec("historical", "H", series={}))
    text, _ = pdf_text(render(bare, mode="AVAILABLE_EVIDENCE", **KW))
    flat = text.replace("\n", " ")
    assert "No annual history is stored" in flat
    assert "No peer group is available" in flat
    assert "No management-discussion text is stored" in flat
    assert "None" not in text


def test_a_gap_is_shown_where_it_matters():
    r = _report()
    r["sections"][1]["gaps"] = [{"label": "CASA ratio", "reason": "no document processed"}]
    assert "Not available: CASA ratio" in _flat(r)


def test_source_register_and_page_furniture():
    data = render(_report(), mode="COMPLETE", **KW)
    text, pages = pdf_text(data)
    assert "Appendix A - Source register" in text and "#9" in text
    doc = pymupdf.open(stream=data, filetype="pdf")
    for i in range(1, doc.page_count):  # every page but the cover
        page_text = doc[i].get_text()
        assert f"page {i + 1} of {pages}" in page_text and "sha aaaaaaaaaaaa" in page_text
        assert "Demo IT Ltd" in page_text  # running header


def test_raw_management_text_is_never_reproduced():
    r = _report()
    r["sections"].append(
        _sec(
            "risks_management",
            "R",
            management_discussion={
                "excerpt": "SECRET RAW EXCERPT TEXT",
                "source": {"page": 212, "evidence_id": 5},
            },
        )
    )
    text, _ = pdf_text(render(r, mode="COMPLETE", **KW))
    assert "SECRET RAW EXCERPT" not in text and "page 212" in text


def test_rendering_is_deterministic_and_does_not_mutate_the_content():
    r = _report()
    before = copy.deepcopy(r)
    assert render(r, mode="COMPLETE", **KW) == render(r, mode="COMPLETE", **KW)
    assert r == before


def test_a_real_generated_report_renders(seeded):  # noqa: F811
    c = complete_company(seeded, "Pro Bank", *BANK)
    report = make_report(seeded, c, rp.COMPLETE)
    data = render(
        report.content,
        version=1,
        mode=report.mode,
        report_id=str(report.id),
        content_hash=report.content_hash,
    )
    text, _ = pdf_text(data)
    assert "Pro Bank" in text and "Appendix C - Methodology" in text
    assert "None" not in text.replace("None established", "")  # no Python placeholders


def test_the_pdf_endpoint_serves_both_layouts(client, seeded):  # noqa: F811
    c = complete_company(seeded, "Style IT", *IT)
    sid = client.post(
        "/api/research/sessions", json={"company_id": str(c.id)}, headers=ALICE
    ).json()["id"]
    rid = client.get(f"/api/research/sessions/{sid}/reports", headers=ALICE).json()[0]["id"]
    pro = pdf_text(client.get(f"/api/research/reports/{rid}/pdf", headers=ALICE).content)[0]
    basic_url = f"/api/research/reports/{rid}/pdf?style=basic"
    basic = pdf_text(client.get(basic_url, headers=ALICE).content)[0]
    assert "EQUITY RESEARCH" in pro and "COMPLETE RESEARCH REPORT" not in pro
    assert "COMPLETE RESEARCH REPORT" in basic
    assert client.get(f"/api/research/reports/{rid}/pdf?style=x", headers=ALICE).status_code == 422


def test_the_stored_axis_report_content_renders_completely():
    path = Path(__file__).parent / "fixtures" / "axis_report_content.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    flat = _flat(c)
    assert "Axis Bank" in flat and "Net interest margin" in flat
    assert "FY2026" in flat and "Appendix C" in flat
    assert "grew 4.0% in FY2026" in flat and "fell 5.8% in FY2026" in flat
