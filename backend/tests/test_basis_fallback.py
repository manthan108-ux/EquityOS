# ruff: noqa: F811
"""Consolidated by default; a clearly warned standalone fallback; the two bases are never mixed."""

from datetime import date

import pytest

from app.docintel.pdf import first_pages_text
from app.research import analysis as an
from app.research import clock
from app.research import readiness as rd
from app.research import report as rp
from app.research.pdf_pro import render
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

pytest.importorskip("pymupdf")
YEARS = [date(2026, 3, 31), date(2025, 3, 31), date(2024, 3, 31), date(2023, 3, 31)]
NAMES = {"INCOME.net_profit", "BALANCE_SHEET.total_assets", "INCOME.revenue_from_operations"}


def company_with(session, name, consolidated=0, standalone=0):
    co = mk_company(session, name)
    for basis, n in (("CONSOLIDATED", consolidated), ("STANDALONE", standalone)):
        for i, end in enumerate(YEARS[:n]):
            for metric in NAMES:
                evidence(session, co, metric, 100 + i, end=end, basis=basis, unit="inr")
    return co


def resolve(session, co, prefer="CONSOLIDATED"):
    return an.resolve_basis(rd.load_store(session, co.id, NAMES), prefer)


def test_consolidated_is_used_when_it_has_statements(seeded):
    co = company_with(seeded, "Both Co", consolidated=4, standalone=4)
    r = resolve(seeded, co)
    assert (r["used"], r["fallback"], r["warning"]) == ("CONSOLIDATED", False, None)
    assert r["annual_years"] == {"CONSOLIDATED": 4, "STANDALONE": 4}


def test_standalone_is_the_fallback_when_consolidated_is_missing(seeded):
    co = company_with(seeded, "Standalone Only Co", consolidated=0, standalone=3)
    r = resolve(seeded, co)
    assert (r["used"], r["fallback"]) == ("STANDALONE", True)
    assert r["requested"] == "CONSOLIDATED"
    assert "Consolidated statements were not available" in r["warning"]
    assert "standalone statements are used throughout" in r["warning"]
    assert "No consolidated and standalone figures are combined" in r["warning"]


def test_one_consolidated_year_is_not_enough_to_stay_on_consolidated(seeded):
    co = company_with(seeded, "Thin Co", consolidated=1, standalone=3)
    r = resolve(seeded, co)
    assert r["used"] == "STANDALONE" and r["annual_years"] == {"CONSOLIDATED": 1, "STANDALONE": 3}


def test_no_fallback_when_the_other_basis_is_no_better(seeded):
    assert not resolve(seeded, company_with(seeded, "Poor Co", 1, 1))["fallback"]
    assert not resolve(seeded, company_with(seeded, "Empty Co", 0, 0))["fallback"]
    assert not resolve(seeded, company_with(seeded, "One Co", 0, 1))["fallback"]


def test_a_requested_standalone_basis_is_respected(seeded):
    co = company_with(seeded, "Both Co 2", consolidated=4, standalone=4)
    r = resolve(seeded, co, "STANDALONE")
    assert (r["used"], r["fallback"]) == ("STANDALONE", False)
    thin = company_with(seeded, "Cons Only Co", consolidated=3, standalone=0)
    back = resolve(seeded, thin, "STANDALONE")
    assert (
        back["used"] == "CONSOLIDATED"
        and "Standalone statements were not available" in back["warning"]
    )


def test_the_report_states_the_basis_it_used_and_warns(seeded):
    co = company_with(seeded, "Report Co", consolidated=0, standalone=4)
    ready = rd.evaluate(seeded, co, today=date(2026, 11, 8))
    content = rp.build_content(seeded, co, ready, rp.AVAILABLE_EVIDENCE, date(2026, 11, 8))
    res = content["meta"]["basis_resolution"]
    assert res["fallback"] and res["used"] == "STANDALONE"
    assert content["limitations"]["basis_notes"][0] == res["warning"]
    if content["analysis"].get("available"):
        assert content["analysis"]["basis"] == "STANDALONE"
        assert content["analysis"]["basis_resolution"]["fallback"] is True
    pdf = render(
        content, version=1, mode=rp.AVAILABLE_EVIDENCE, report_id="0" * 36, content_hash="0" * 64
    )
    cover = first_pages_text(pdf, 2)[0]
    assert "Basis warning" in cover and "Consolidated statements were not available" in cover
    assert "Standalone" in cover  # the accounting basis row names what was used


def test_a_company_with_consolidated_data_gets_no_warning(seeded):
    co = company_with(seeded, "Normal Co", consolidated=4, standalone=2)
    ready = rd.evaluate(seeded, co, today=date(2026, 11, 8))
    content = rp.build_content(seeded, co, ready, rp.AVAILABLE_EVIDENCE, clock.today())
    assert content["meta"]["basis_resolution"]["fallback"] is False
    assert not any(
        "not available for this company" in n for n in content["limitations"]["basis_notes"]
    )
    pdf = render(
        content, version=1, mode=rp.AVAILABLE_EVIDENCE, report_id="0" * 36, content_hash="0" * 64
    )
    assert "Basis warning" not in first_pages_text(pdf, 2)[0]


def test_the_analysis_never_mixes_bases(seeded):
    """With a fallback every annual point in the trend is on the one basis used."""
    co = company_with(seeded, "Mixed Co", consolidated=1, standalone=4)
    ready = rd.evaluate(seeded, co, today=date(2026, 11, 8))
    content = rp.build_content(seeded, co, ready, rp.AVAILABLE_EVIDENCE, date(2026, 11, 8))
    analysis = content["analysis"]
    if not analysis.get("available"):
        pytest.skip("the analytical layer needs more than this synthetic company provides")
    bases = {
        p["basis"]
        for row in analysis["trend"]["annual"]
        for p in row.get("points", [])
        if p.get("basis")
    }
    assert bases <= {"STANDALONE"}
