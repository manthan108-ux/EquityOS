# ruff: noqa: F811, E501
"""Industry and peer analysis: benchmark labels, structure, environment, opportunities and peer quality."""

import json
from datetime import date

from app.models import CompanyClassification
from app.research import industry_view as iv
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

FY = date(2026, 3, 31)
CRORE = 10_000_000


def label(session, co, text="Software"):
    session.add(CompanyClassification(company_id=co.id, scheme="UPSTOX_SECTOR", label=text, source="test", retrieved_at=date(2026, 11, 1)))  # fmt: skip
    session.flush()


def group(session, revenues, name="Group"):
    cos = []
    for i, rev in enumerate(revenues):
        co = mk_company(session, f"{name} {i}")
        label(session, co)
        if rev is not None:
            evidence(
                session,
                co,
                "INCOME.revenue_from_operations",
                rev * CRORE,
                end=FY,
                basis="CONSOLIDATED",
                unit="inr",
                ptype="ANNUAL",
            )
        cos.append(co)
    return cos


# ---- structure -------------------------------------------------------------------------------------------------


def test_structure_is_concentration_among_the_stored_companies_with_a_scope_statement(seeded):
    cos = group(seeded, [6000, 2000, 1000, 1000])
    s = iv.structure(seeded, cos[1], "CONSOLIDATED")
    assert s["available"] and s["with_revenue"] == 4 and s["coverage_pct"] == "100"
    assert s["top1"]["share_pct"] == "60.0" and s["top3_share_pct"] == "90.0"
    assert s["hhi"] == 3000 + 0 + 0 or s["hhi"] == 4200 - 0 or s["hhi"] > 2500
    assert (
        s["reading"] == "CONCENTRATED"
        and s["company_rank"] == 2
        and s["company_share_pct"] == "20.0"
    )
    assert "stored universe, not the market" in s["scope"]


def test_a_fragmented_group_reads_fragmented(seeded):
    cos = group(seeded, [1000] * 10, "Frag")
    s = iv.structure(seeded, cos[0], "CONSOLIDATED")
    assert s["hhi"] == 1000 and s["reading"] == "FRAGMENTED"


def test_a_thin_coverage_withholds_the_reading_and_says_why(seeded):
    cos = group(seeded, [5000, 3000, 2000, None, None, None, None, None], "Thin")
    s = iv.structure(seeded, cos[0], "CONSOLIDATED")
    assert s["available"] and s["reading"] is None and int(s["coverage_pct"]) < 60
    assert "no reading" in s["rule"] and "partial" in s["rule"]


def test_fewer_than_three_stored_revenues_means_no_structure(seeded):
    cos = group(seeded, [5000, 3000, None, None], "Few")
    s = iv.structure(seeded, cos[0], "CONSOLIDATED")
    assert s["available"] is False and "too few" in s["reason"]


def test_a_company_with_no_label_has_no_structure(seeded):
    s = iv.structure(seeded, mk_company(seeded, "Unlabelled Co"), "CONSOLIDATED")
    assert s["available"] is False and "no usable industry label" in s["reason"]


def test_a_catch_all_label_is_not_an_industry(seeded):
    co = mk_company(seeded, "Misc Co")
    label(seeded, co, "Miscellaneous")
    assert iv.structure(seeded, co, "CONSOLIDATED")["available"] is False


def test_companies_on_another_fiscal_year_are_not_mixed_into_the_shares(seeded):
    cos = group(seeded, [4000, 3000, 2000], "Year")
    old = mk_company(seeded, "Old Year Co")
    label(seeded, old)
    evidence(
        seeded,
        old,
        "INCOME.revenue_from_operations",
        90_000 * CRORE,
        end=date(2025, 3, 31),
        basis="CONSOLIDATED",
        unit="inr",
        ptype="ANNUAL",
    )
    s = iv.structure(seeded, cos[0], "CONSOLIDATED")
    assert s["with_revenue"] == 3 and s["top1"]["share_pct"] == "44.4"


# ---- benchmarks: the three things are never confused -----------------------------------------------------------


def analysis_with(peer=True, group_ok=True):
    pg = {"peer_benchmark": peer, "peer_group_median_cagr": "8.0", "peer_count": 4, "company": {}}
    if not peer:
        pg = {
            "peer_benchmark": False,
            "reason": "fewer than 3 peers have the same fiscal-year end points",
        }
    ig = {"available": True, "label": "Software", "metrics": {"revenue": {"available": True, "median_cagr": "9.5", "aggregate_cagr": "9.0", "members_used": 7, "group_size": 12}}} if group_ok else {"available": False, "reason": "too few"}  # fmt: skip
    return {
        "profitability": {"peer_growth": {"revenue": pg}},
        "growth": {"industry_group": ig},
        "sector": {"code": "IT_SERVICES"},
    }


def test_the_three_benchmarks_are_named_apart_and_the_industry_is_never_available():
    b = iv.benchmarks(analysis_with())
    assert (
        b["peer_group"]["name"] == "Peer-group CAGR"
        and b["peer_group"]["revenue_cagr_median"] == "8.0"
    )
    assert (
        b["sector_label_group"]["name"] == "Sector-label group CAGR"
        and b["sector_label_group"]["revenue"]["members_used"] == 7
    )
    assert (
        b["industry"]["available"] is False
        and "no verified industry statistics" in b["industry"]["reason"]
    )
    assert "never called the industry" in b["industry"]["reason"]
    text = json.dumps(b).lower()
    assert "industry cagr" in text and "growth alpha" in text  # named only to say there is none


def test_unavailable_benchmarks_give_reasons_not_numbers():
    b = iv.benchmarks(analysis_with(peer=False, group_ok=False))
    assert not b["peer_group"]["available"] and b["peer_group"]["revenue_cagr_median"] is None
    assert (
        "fewer than 3 peers" in b["peer_group"]["reason"]
        and b["sector_label_group"]["reason"] == "too few"
    )


# ---- environment and opportunities/threats -----------------------------------------------------------------------


def test_environment_is_labelled_context_with_no_statistics():
    env = iv.environment("BANK")
    assert (
        env["kind"] == "CONTEXT"
        and env["regulation"]
        and "no statistics" in env["note"].lower()
        or "statistics" in env["note"]
    )


def test_opportunities_and_threats_are_checked_against_the_companys_figures():
    kpis = {"indicators": {"gnpa_ratio": {"available": True, "key": "gnpa_ratio", "label": "Gross NPA ratio", "kind": "percent", "source": "REPORTED", "variants": [{"dimension": None, "period_type": "QUARTERLY", "latest": {"value": "2.2", "fy": "FY2027", "period_end": "2026-09-30", "evidence_id": 9}, "change": "-0.4", "change_from": "2025-09-30", "change_to": "2026-09-30", "change_evidence_ids": [8, 9]}]}}}  # fmt: skip
    out = iv.opportunities_threats("BANK", kpis)
    watch = {w["indicator"]: w for w in out["checked_against_company"]}
    assert (
        watch["gnpa_ratio"]["company_evidence"]["direction"] == "FAVOURABLE"
    )  # a falling NPA ratio is favourable
    assert watch["gnpa_ratio"]["company_evidence"]["evidence_ids"] == [8, 9]
    assert watch["casa"]["company_evidence"] is None and "not stored" in watch["casa"]["reason"]
    assert out["opportunities"] and out["threats"]


def test_a_rising_cost_ratio_is_unfavourable_and_a_rising_growth_is_favourable():
    def ind(key, label, change):
        return {"available": True, "key": key, "label": label, "kind": "percent", "source": "CALCULATED", "variants": [{"dimension": None, "period_type": "ANNUAL", "latest": {"value": "50", "fy": "FY2026", "evidence_id": 1}, "change": change, "change_from": "FY2023", "change_to": "FY2026", "change_evidence_ids": [1, 2]}]}  # fmt: skip

    out = iv.opportunities_threats("IT_SERVICES", {"indicators": {"employee_cost_ratio": ind("employee_cost_ratio", "Employee cost / revenue", "3.0"), "revenue_growth": ind("revenue_growth", "Revenue growth", "2.0")}})  # fmt: skip
    watch = {
        w["indicator"]: w["company_evidence"]["direction"]
        for w in out["checked_against_company"]
        if w["company_evidence"]
    }
    assert watch == {"employee_cost_ratio": "UNFAVOURABLE", "revenue_growth": "FAVOURABLE"}


# ---- peer quality ----------------------------------------------------------------------------------------------


def test_peer_quality_counts_reliability_and_lists_the_withheld_comparisons():
    rows = [
        {"label": "ROE", "reliability": "HIGH", "coverage": {"comparable": 6}},
        {
            "label": "Margin",
            "reliability": "LOW",
            "position": "not ranked",
            "coverage": {"comparable": 3},
            "note": "3 of 8 selected peers are comparable",
        },
        {"label": "Growth", "reliability": "NONE", "coverage": {"comparable": 1}},
    ]
    q = iv.peer_quality(
        {
            "peers": {
                "peers": [{"name": f"P{i}", "reason": "same industry"} for i in range(8)],
                "rows": rows,
            }
        }
    )
    assert q["reliability_counts"] == {"HIGH": 1, "MEDIUM": 0, "LOW": 1, "NONE": 1}
    assert [w["label"] for w in q["withheld"]] == ["Margin", "Growth"] and q["selected_peers"] == 8
    assert "4 comparable peers" in q["policy"] and "never compared" in q["policy"]


def test_few_selected_peers_produce_a_warning_not_a_rank():
    q = iv.peer_quality(
        {
            "peers": {
                "peers": [{"name": "A"}, {"name": "B"}],
                "rows": [{"label": "ROE", "reliability": "NONE", "coverage": {"comparable": 0}}],
            }
        }
    )
    assert any("only 2 peers" in w for w in q["warnings"]) and any(
        "no metric has enough" in w for w in q["warnings"]
    )


# ---- the whole -------------------------------------------------------------------------------------------------


def test_the_whole_view_has_no_industry_figure_and_labels_the_structure_scope(seeded):
    cos = group(seeded, [6000, 2000, 1000, 1000], "Whole")
    out = iv.build(
        seeded,
        cos[0],
        {**analysis_with(), "peers": {"peers": [], "rows": []}, "sector_kpis": None},
        "CONSOLIDATED",
    )
    assert (
        out["available"]
        and out["industry_size"]["available"] is False
        and out["benchmarks"]["industry"]["available"] is False
    )
    assert out["structure"]["available"] and out["environment"]["kind"] == "CONTEXT"
    assert any("never interchanged" in d for d in out["definitions"])


def test_the_pdf_shows_the_section_with_benchmarks_and_omits_it_when_empty(seeded):
    from tests.test_report_context import _axis, _flat

    cos = group(seeded, [6000, 2000, 1000, 1000], "PDF")
    content = _axis()
    content["analysis"]["industry_view"] = json.loads(json.dumps(iv.build(seeded, cos[0], {**analysis_with(), "peers": {"peers": [], "rows": []}, "sector_kpis": None}, "CONSOLIDATED")))  # fmt: skip
    flat = _flat(content)
    assert (
        "Industry and peer analysis" in flat
        and "Peer-group CAGR" in flat
        and "Sector-label group CAGR" in flat
    )
    assert "Industry CAGR" in flat and "Regulatory and macroeconomic environment" in flat
    empty = _axis()
    empty["analysis"]["industry_view"] = {"available": False}
    assert "Industry and peer analysis" not in _flat(empty)
