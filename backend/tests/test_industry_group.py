"""Industry-group growth: provider sector label, same fiscal years, same basis, honest coverage."""

# ruff: noqa: F811
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from app.models import CompanyClassification
from app.research import industry
from app.research import readiness as rd
from tests.research_helpers import evidence, mk_company
from tests.test_report_context import FY, IT, _stores
from tests.test_sector import seeded  # noqa: F401

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def label(session, company, text="Software", cap=2000000):
    session.add(
        CompanyClassification(
            company_id=company.id,
            scheme="UPSTOX_SECTOR",
            label=text,
            group_market_cap_crore=cap,
            retrieved_at=NOW,
            source="test",
        )
    )


def revenue(session, company, first, last, basis="CONSOLIDATED"):
    for end, v in zip((FY[0], FY[3]), (first, last), strict=True):
        evidence(
            session,
            company,
            "INCOME.revenue_from_operations",
            v,
            end=end,
            ptype="ANNUAL",
            basis=basis,
        )


def group(session, n_members=5, other_label=0, wrong_basis=0):
    own = mk_company(session, "Own", *IT)
    label(session, own)
    revenue(session, own, 100e9, 133.1e9)  # 10% a year over 3 years
    ends = (121e9, 133.1e9, 146.41e9, 110e9, 160e9, 90e9, 100e9)
    for i in range(n_members):
        m = mk_company(session, f"Member {i}", *IT)
        label(session, m)
        revenue(session, m, 100e9, ends[i])
    for i in range(other_label):
        o = mk_company(session, f"Elsewhere {i}", *IT)
        label(session, o, "Cement")
        revenue(session, o, 100e9, 500e9)
    for i in range(wrong_basis):
        w = mk_company(session, f"Standalone {i}", *IT)
        label(session, w)
        revenue(session, w, 100e9, 500e9, basis="STANDALONE")
    session.commit()
    return own


def stats(session, own, prefer="CONSOLIDATED"):
    names = {"INCOME.revenue_from_operations", "INCOME.total_income", "INCOME.net_profit"}
    store = rd.load_store(session, own.id, names)
    return industry.industry_group(session, own, store, prefer, "IT_SERVICES")


def test_group_cagr_aggregate_and_alpha_match_hand_calculation(seeded):
    own = group(seeded)
    out = stats(seeded, own)
    r = out["metrics"]["revenue"]
    assert out["available"] and r["available"] and r["members_used"] == 5
    assert r["company"]["cagr"] == "10" and r["company"]["years"] == 3
    # combined: (121+133.1+146.41+110+160)/(5*100) = 670.51/500 over 3 years
    expected = ((D("670.51") / D("500")) ** (D(1) / D(3)) - 1) * 100
    assert abs(D(r["aggregate_cagr"]) - expected) < D("0.05")
    assert D(r["alpha_vs_aggregate_pp"]) == D(r["company"]["cagr"]) - D(r["aggregate_cagr"])
    assert D(r["alpha_vs_median_pp"]) == D(r["company"]["cagr"]) - D(r["median_cagr"])
    assert all(m["evidence_ids"] for m in r["members"])  # every member's figures are traceable


def test_the_group_is_the_providers_label_and_its_source_and_date_are_stated(seeded):
    out = stats(seeded, group(seeded, other_label=3))
    assert out["label"] == "Software" and out["group_companies"] == 6  # 'Cement' ones excluded
    assert "13/11/2026" in out["classification_source"].replace("2026-11-13", "13/11/2026")
    assert "provider's label" in out["classification_source"]
    assert out["group_market_cap_crore"] == "2000000"
    assert "Elsewhere" not in str(out["metrics"])


def test_the_company_is_not_its_own_benchmark_and_other_bases_are_not_mixed(seeded):
    out = stats(seeded, group(seeded, wrong_basis=3))
    r = out["metrics"]["revenue"]
    assert r["members_used"] == 5 and not any("Standalone" in m["name"] for m in r["members"])
    assert not any(m["name"] == "Own" for m in r["members"])


def test_fewer_than_five_comparable_companies_gives_no_statistic(seeded):
    out = stats(seeded, group(seeded, n_members=4))
    r = out["metrics"]["revenue"]
    assert r["available"] is False and "fewer than 5 other companies" in r["reason"]
    assert "aggregate_cagr" not in r and out["available"] is False


def test_a_company_without_a_label_says_so(seeded):
    own = mk_company(seeded, "Unlabelled", *IT)
    revenue(seeded, own, 100e9, 130e9)
    seeded.commit()
    out = stats(seeded, own)
    assert out["available"] is False and "no industry label" in out["reason"]


def test_the_build_attaches_the_group_to_the_growth_block(seeded):
    own = group(seeded)
    (_, store), *_ = _stores(seeded, [own])
    out = industry.build(seeded, own, store, [], "IT_SERVICES", "CONSOLIDATED", [])
    assert out["growth"]["industry_group"]["metrics"]["revenue"]["available"] is True


@pytest.mark.parametrize("available", [True, False])
def test_the_report_shows_the_group_or_says_why_not(seeded, available):
    from tests.test_report_context import _axis, _flat

    own = group(seeded) if available else group(seeded, n_members=2)
    store = rd.load_store(seeded, own.id, {"INCOME.revenue_from_operations", "INCOME.net_profit"})
    content = _axis()
    content["analysis"]["growth"]["industry_group"] = industry.industry_group(
        seeded, own, store, "CONSOLIDATED", "IT_SERVICES"
    )
    text = _flat(content)
    assert "Industry-group comparison (provider sector label)" in text
    if available:
        assert "Group: Software (6 listed companies carry this label" in text
        assert "not the whole industry" in text and "group members used" in text
    else:
        assert "fewer than 5 other companies" in text
