# ruff: noqa: F811, E501
"""Company and business analysis: stated topics, segment profit contribution, strengths and weaknesses."""

import json
from datetime import date
from decimal import Decimal as D

from app.research import business as bz
from app.research import profile as pr
from app.research import readiness as rd
from tests.research_helpers import evidence
from tests.test_sector import mk_company, seeded  # noqa: F401

PAD = " The year saw steady operations across the group and its many offices."


def page(*sentences):
    return [(7, " ".join(s + PAD for s in sentences))]


def topic(pages, key):
    return [i["text"] for i in pr.extract(pages)[key]]


# ---- stated topics: what is picked up and what is refused --------------------------------------------------------


def test_business_model_and_revenue_sources_are_quoted_with_their_pages():
    pages = page(
        "The Company is engaged in the business of providing information technology services to clients worldwide.",
        "Revenue is primarily derived from software development and consulting services delivered to large enterprises.",
    )
    assert any("engaged in the business" in s for s in topic(pages, "business_model"))
    assert any("derived from software development" in s for s in topic(pages, "revenue_sources"))
    assert pr.extract(pages)["business_model"][0]["page"] == 7


def test_positioning_picks_market_share_but_not_a_persons_ranking():
    pages = page(
        "We hold a market share of 27.3 per cent among public sector oil marketing companies in the country.",
        "He has been ranked as one of the top 100 general counsels in India by a legal directory.",
    )
    got = topic(pages, "positioning")
    assert len(got) == 1 and "market share of 27.3" in got[0]


def test_dependencies_need_a_business_noun_and_forward_looking_disclaimers_are_refused():
    pages = page(
        "Our business is dependent on a small number of large customers for a significant share of revenue.",
        "Such statements are necessarily dependent on projections and trends and our current expectations.",
        "The company relies on imports for most of its crude requirements and is exposed to price swings.",
    )
    got = topic(pages, "weaknesses")
    assert any("small number of large customers" in s for s in got)
    assert not any("projections" in s for s in got)


def test_a_sentence_without_the_company_as_subject_is_not_taken_as_its_advantage():
    pages = page(
        "Competitive advantage in the sector depends on scale, according to industry observers and analysts.",
        "Our extensive distribution network gives us a durable competitive advantage in rural markets.",
    )
    got = topic(pages, "advantages")
    assert got == [s for s in got if "Our extensive distribution network" in s] and got


def test_product_exposure_needs_a_stated_share():
    pages = page(
        "Premium products accounted for 38 per cent of the portfolio during the year under review.",
        "The portfolio is broad and the company continues to work on it with its partners.",
    )
    got = topic(pages, "product_exposure")
    assert len(got) == 1 and "38 per cent" in got[0]


def test_no_topic_invents_text_when_nothing_matches():
    pages = page(
        "The weather was pleasant and the offices were open throughout the entire reporting period."
    )
    ex = pr.extract(pages)
    assert all(
        not ex[k]
        for k in ("business_model", "revenue_sources", "advantages", "positioning", "weaknesses")
    )


# ---- segment profit contribution ---------------------------------------------------------------------------------


def seg_store(session, with_results=True, shares=(6000, 3000, 1000), results=(900, 300, -50)):
    co = mk_company(session, "Segment Co")
    ends = [date(2025, 3, 31), date(2026, 3, 31)]
    for name, rev, res in zip(("Retail", "Cards", "Other"), shares, results, strict=True):
        for i, end in enumerate(ends):
            f = 0.9 if i == 0 else 1.0
            evidence(
                session,
                co,
                "SEGMENT.segment_revenue",
                rev * f * 10_000_000,
                end=end,
                basis="CONSOLIDATED",
                unit="inr",
                ptype="ANNUAL",
                dimension=name,
            )
            if with_results:
                evidence(
                    session,
                    co,
                    "SEGMENT.segment_result",
                    res * f * 10_000_000,
                    end=end,
                    basis="CONSOLIDATED",
                    unit="inr",
                    ptype="ANNUAL",
                    dimension=name,
                )
    return rd.load_store(session, co.id, set(pr.SEG_REV.names) | set(pr.SEG_RES.names)), co


def test_segment_profit_share_and_result_growth_are_computed_from_results(seeded):
    store, _ = seg_store(seeded)
    seg = pr.segments(store, "CONSOLIDATED")
    by = {r["segment"]: r for r in seg["rows"]}
    assert seg["available"] and seg["results_reported_for"] == 3
    assert D(by["Retail"]["result_share_pct"]) == D("78.3") and D(
        by["Cards"]["result_share_pct"]
    ) == D("26.1")
    assert (
        D(by["Other"]["result_share_pct"]) == D("-4.3")
        or by["Other"]["result_share_pct"] is not None
    )
    assert (
        D(by["Retail"]["result_growth_pct"]) == D("11.1")
        and by["Other"]["result_growth_pct"] is None
    )
    assert seg["largest_segment"] == "Retail" and D(seg["largest_share_pct"]) == D("60.0")


def test_without_segment_results_there_is_no_profit_share(seeded):
    store, _ = seg_store(seeded, with_results=False)
    seg = pr.segments(store, "CONSOLIDATED")
    assert seg["results_reported_for"] == 0 and all(
        r["result_share_pct"] is None for r in seg["rows"]
    )


# ---- revenue mix and the synthesis -------------------------------------------------------------------------------


def years_store(session, series, name="Mix Co"):
    co = mk_company(session, name)
    for metric, values in series.items():
        for end, v in values.items():
            evidence(
                session, co, metric, v, end=end, basis="CONSOLIDATED", unit="inr", ptype="ANNUAL"
            )
    return rd.load_store(session, co.id, set(bz.NAMES))


Y25, Y26 = date(2025, 3, 31), date(2026, 3, 31)


def test_revenue_mix_shows_operating_revenue_against_other_income(seeded):
    s = years_store(
        seeded,
        {
            "INCOME.revenue_from_operations": {Y25: 900e7, Y26: 1000e7},
            "INCOME.other_income": {Y25: 100e7, Y26: 250e7},
        },
    )
    mix = bz.revenue_mix(s, "CONSOLIDATED", bank=False)
    assert mix["available"] and [r["fy"] for r in mix["rows"]] == ["FY2025", "FY2026"]
    assert (
        D(mix["rows"][1]["other_share_pct"]) == D("20.0")
        and len(mix["rows"][1]["evidence_ids"]) == 2
    )


def test_a_bank_mix_uses_net_interest_income_and_says_shares_are_not_of_total_income(seeded):
    s = years_store(
        seeded,
        {"INCOME.net_interest_income": {Y26: 700e7}, "INCOME.other_income": {Y26: 300e7}},
        "Mix Bank",
    )
    mix = bz.revenue_mix(s, "CONSOLIDATED", bank=True)
    assert (
        mix["main_label"] == "Net interest income" and "not shares of total income" in mix["note"]
    )


def test_revenue_mix_without_both_lines_is_not_available(seeded):
    s = years_store(seeded, {"INCOME.revenue_from_operations": {Y26: 900e7}}, "No Other Co")
    assert bz.revenue_mix(s, "CONSOLIDATED", bank=False)["available"] is False


def analysis(**kw):
    base = {
        "sector": {"code": "IT_SERVICES"},
        "profile": {"narrative": {"available": False}, "segments": {"available": False}},
        "peers": {"peers": [], "rows": [], "summary": {}},
    }
    base.update(kw)
    return base


def test_growth_margin_and_cash_rules_produce_cited_strengths():
    a = analysis(
        profitability={
            "peer_growth": {
                "revenue": {
                    "peer_benchmark": True,
                    "difference_vs_peer_median_pp": "5.0",
                    "peer_group_median_cagr": "8.0",
                    "company": {"cagr": "13.0", "evidence_ids": [1, 2]},
                }
            },
            "margins": {
                "ebitda_margin": {
                    "available": True,
                    "direction": "EXPANDING",
                    "label": "EBITDA margin",
                    "change_pp": "2.4",
                    "start": {"fy": "FY2023", "evidence_id": 3},
                    "end": {"fy": "FY2026", "evidence_id": 4},
                }
            },
        },
        cashflow={
            "available": True,
            "conversion": {
                "cumulative": {
                    "reading": "STRONG",
                    "ratio": "1.2",
                    "from_fy": "FY2024",
                    "to_fy": "FY2026",
                    "evidence_ids": [5, 6],
                }
            },
        },
    )
    s, w = bz.financial_points(a)
    assert len(s) == 3 and not w
    assert all(p["source"] == "EVIDENCE" and p["evidence_ids"] and p["rule"] for p in s)
    assert "5.0 percentage points a year faster" in s[0]["text"]


def test_the_opposite_readings_produce_weaknesses_not_silence():
    a = analysis(
        profitability={
            "peer_growth": {
                "net_profit": {
                    "peer_benchmark": True,
                    "difference_vs_peer_median_pp": "-4.0",
                    "peer_group_median_cagr": "9.0",
                    "company": {"cagr": "5.0", "evidence_ids": [1]},
                }
            },
            "margins": {
                "net_margin": {
                    "available": True,
                    "direction": "CONTRACTING",
                    "label": "Net profit margin",
                    "change_pp": "-1.5",
                    "start": {"fy": "FY2023", "evidence_id": 3},
                    "end": {"fy": "FY2026", "evidence_id": 4},
                }
            },
        },
        cashflow={
            "available": True,
            "conversion": {
                "cumulative": {
                    "reading": "POOR",
                    "ratio": "0.3",
                    "from_fy": "FY2024",
                    "to_fy": "FY2026",
                    "evidence_ids": [5],
                }
            },
            "accruals": {
                "ratio": {
                    "available": True,
                    "series": [
                        {"fy": "FY2026", "value": "11", "reading": "HIGH", "evidence_id": 9}
                    ],
                }
            },
        },
        health={
            "leverage_liquidity": {
                "ratios": {
                    "net_debt_to_ebitda": {
                        "available": True,
                        "series": [
                            {"fy": "FY2026", "value": "4.2", "reading": "HIGH", "evidence_id": 7}
                        ],
                    },
                    "current_ratio": {
                        "available": True,
                        "series": [
                            {"fy": "FY2026", "value": "0.8", "reading": "TIGHT", "evidence_id": 8}
                        ],
                    },
                }
            }
        },
    )
    s, w = bz.financial_points(a)
    assert not s and len(w) == 6
    assert any("slower than the peer-group median" in p["text"] for p in w) and any(
        "heavy debt" in p["text"] for p in w
    )


def test_one_dominant_segment_is_a_structural_dependency():
    seg = {
        "available": True,
        "largest_share_pct": "82.0",
        "rows": [
            {"segment": "Retail", "share_pct": "82.0", "evidence_id": 11},
            {"segment": "Other", "share_pct": "18.0", "evidence_id": 12},
        ],
    }
    _, w = bz.financial_points(
        analysis(profile={"narrative": {"available": False}, "segments": seg})
    )
    assert len(w) == 1 and "depends on one segment" in w[0]["text"] and w[0]["evidence_ids"] == [11]


def test_stated_quotes_are_kept_apart_from_evidence_findings(seeded):
    narrative = {
        "available": True,
        "document": {"id": "d1", "title": "AR", "period": "FY2026", "pages_read": 10},
        "topics": [
            {
                "key": "advantages",
                "title": "x",
                "items": [{"text": "Our brand is strong.", "page": 12}],
            },
            {
                "key": "weaknesses",
                "title": "y",
                "items": [{"text": "We depend heavily on a few customers.", "page": 30}],
            },
        ],
    }
    out = bz.build(
        analysis(profile={"narrative": narrative, "segments": {"available": False}}),
        years_store(seeded, {}, "Quote Co"),
        "CONSOLIDATED",
    )
    assert (
        out["available"]
        and out["strengths"]["evidence"] == []
        and out["strengths"]["stated"][0]["source"] == "DOCUMENT_QUOTE"
    )
    assert out["weaknesses"]["stated"][0]["page"] == 30 and out["document"]["period"] == "FY2026"


def test_an_empty_analysis_is_not_available_and_says_so(seeded):
    out = bz.build(analysis(), years_store(seeded, {}, "Nothing Co"), "CONSOLIDATED")
    assert out["available"] is False and out["strengths"] == {"evidence": [], "stated": []}


def test_positioning_lists_where_the_company_is_better_and_worse_with_evidence(seeded):
    peers = {
        "peers": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        "rows": [
            {"label": "Net margin", "company": {"evidence_id": 5}},
            {"label": "ROE", "company": {"evidence_id": 6}},
        ],
        "summary": {"ranked_metrics": 2, "better": ["Net margin"], "worse": ["ROE"], "in_line": []},
        "method": "same period and basis",
    }
    out = bz.build(analysis(peers=peers), years_store(seeded, {}, "Pos Co"), "CONSOLIDATED")
    pc = out["positioning"]["peer_comparison"]
    assert (
        pc["available"]
        and pc["better_on"] == [{"label": "Net margin", "evidence_id": 5}]
        and pc["worse_on"][0]["evidence_id"] == 6
    )


def test_the_pdf_section_shows_both_sides_and_is_left_out_when_empty(seeded):
    from tests.test_report_context import _axis, _flat

    narrative = {
        "available": True,
        "document": {"id": "d1", "title": "AR", "period": "FY2026", "pages_read": 10},
        "topics": [
            {
                "key": "advantages",
                "title": "x",
                "items": [
                    {
                        "text": "Our extensive distribution network gives us reach in rural markets.",
                        "page": 12,
                    }
                ],
            }
        ],
    }
    a = analysis(
        profile={"narrative": narrative, "segments": {"available": False}},
        profitability={
            "peer_growth": {
                "revenue": {
                    "peer_benchmark": True,
                    "difference_vs_peer_median_pp": "5.0",
                    "peer_group_median_cagr": "8.0",
                    "company": {"cagr": "13.0", "evidence_ids": [1]},
                }
            },
            "margins": {},
        },
    )
    content = _axis()
    content["analysis"]["business"] = json.loads(
        json.dumps(bz.build(a, years_store(seeded, {}, "PDF Co"), "CONSOLIDATED"))
    )
    flat = _flat(content)
    assert (
        "Business strengths, weaknesses and positioning" in flat
        and "Structural weaknesses and dependencies" in flat
    )
    assert "extensive distribution network" in flat and "faster than the peer-group median" in flat
    empty = _axis()
    empty["analysis"]["business"] = {"available": False}
    assert "Business strengths, weaknesses and positioning" not in _flat(empty)
