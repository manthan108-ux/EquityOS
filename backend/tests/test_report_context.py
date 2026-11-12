"""Report quality: DD/MM/YYYY dates, no inline evidence labels, the industry overview, company profile,
growth comparison (CAGR, peer-set CAGR, growth alpha), cyclicality, and the contents page."""

import json
import re
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.research import cyclicality, industry, profile
from app.research import readiness as rd
from app.research.pdf import dmy
from app.research.pdf_pro import render
from app.research.sectors import SECTORS
from tests.research_helpers import evidence, mk_company
from tests.test_pdf_pro import KW, _report
from tests.test_research_pdf import BANK as BANK_SECTOR
from tests.test_research_pdf import IT, pdf_text
from tests.test_sector import seeded  # noqa: F401

pymupdf = pytest.importorskip("pymupdf")
FY = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
FIXTURE = Path(__file__).parent / "fixtures" / "axis_report_v2.json"


def _axis():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _flat(content, mode="COMPLETE"):
    data = render(content, mode=mode, **KW)
    return pdf_text(data)[0].replace("\n", " ")


# ---- dates ----------------------------------------------------------------------------------------


def test_dmy_rewrites_iso_dates_only():
    assert (
        dmy("period ended 2026-03-31, price on 2026-11-06")
        == "period ended 31/03/2026, price on 06/11/2026"
    )
    assert (
        dmy("FY2026, p.7, evidence #5320, sha 3134f0a2-479d")
        == "FY2026, p.7, evidence #5320, sha 3134f0a2-479d"
    )
    assert dmy("v2026-03-3") == "v2026-03-3"  # not a date


def test_the_whole_pdf_uses_ddmmyyyy_and_stored_dates_are_untouched():
    content = _axis()
    before = json.dumps(content, sort_keys=True)
    text = pdf_text(render(content, mode="COMPLETE", **KW))[0]
    assert not ISO.findall(text), set(ISO.findall(text))
    assert len(re.findall(r"\b\d{2}/\d{2}/\d{4}\b", text)) > 50
    assert "11/11/2026" in text  # the report date, in the cover and running header
    assert json.dumps(content, sort_keys=True) == before  # display only: stored dates are unchanged
    assert content["meta"]["as_of"] == "2026-11-11"


def test_the_plain_register_pdf_also_uses_ddmmyyyy():
    from app.research.pdf import render as basic

    text = pdf_text(basic(_axis(), mode="COMPLETE", **KW))[0]
    assert not ISO.findall(text) and "11/11/2026" in text


# ---- inline evidence labels removed, traceability kept ---------------------------------------------


def test_no_status_labels_beside_facts_but_citations_and_notes_remain():
    flat = _flat(_axis())
    for label in (
        "VERIFIED FACT",
        "[DERIVED",
        "[INTERPRETATION",
        "[MANAGEMENT STATEMENT",
        "[ASSUMPTION",
        "[SECTOR TEMPLATE",
        "(DERIVED)",
    ):
        assert label not in flat, label
    assert flat.count("evidence #") > 30 and "p.7" in flat  # traceability is intact
    assert (
        "calculated, evidence #" in flat
    )  # reported versus calculated stays visible in the source note
    assert (
        "Appendix E - Data quality and how statements are classified" in flat
    )  # the classification lives here
    assert "Hypothesis:" in flat  # a hypothesis is called a hypothesis in the sentence itself
    assert "illustrative" in flat  # valuation assumptions keep their qualification


# ---- contents and structure -----------------------------------------------------------------------


def test_the_contents_page_lists_every_section_with_page_numbers():
    data = render(_axis(), mode="COMPLETE", **KW)
    doc = pymupdf.open(stream=data, filetype="pdf")
    toc = doc[1].get_text()
    assert toc.startswith("Contents")
    for title in (
        "Industry overview",
        "Company and management profile",
        "Growth versus the industry",
        "Business cyclicality",
        "Peer benchmarking",
        "Valuation analysis",
        "Appendix A - Source register",
    ):
        assert title in toc
    # the number printed in the contents is the page the heading is on
    heading_page = next(
        i + 1
        for i, p in enumerate(doc)
        if i > 1 and p.get_text().lstrip().startswith("6. Business cyclicality")
    )
    # (the text extractor lists a right-aligned page number just before its title)
    assert re.search(rf"\b{heading_page}\s+6\. Business cyclicality", toc.replace("\n", " "))


def test_every_page_but_the_cover_has_header_footer_and_page_numbers():
    data = render(_axis(), mode="COMPLETE", **KW)
    doc = pymupdf.open(stream=data, filetype="pdf")
    for i in range(1, doc.page_count):
        t = doc[i].get_text()
        assert f"page {i + 1} of {doc.page_count}" in t and "Axis Bank" in t


# ---- industry overview ----------------------------------------------------------------------------


def test_every_sector_has_a_template_and_the_template_contains_no_statistics():
    for code in (
        "BANK",
        "IT_SERVICES",
        "FMCG",
        "AUTOMOBILES",
        "ENERGY",
        "GENERIC_NONFIN",
        "CONGLOMERATE",
    ):
        t = industry.template(code)
        text = json.dumps({k: v for k, v in t.items() if k != "sensitivity"})
        assert not re.search(r"\d", text), (
            code,
            re.findall(r".{20}\d.{10}", text)[:2],
        )  # no invented numbers
        for key in (
            "background",
            "how_it_earns",
            "revenue_drivers",
            "structure",
            "demand",
            "challenges",
            "regulation",
            "cyclical_prior",
            "leading_indicators",
            "sensitivity",
        ):
            assert t[key], (code, key)
    assert industry.template("BANK")["background"] != industry.template("FMCG")["background"]
    assert set(SECTORS) >= {"BANK", "IT_SERVICES", "FMCG", "AUTOMOBILES", "ENERGY"}


def test_the_industry_section_is_labelled_as_context_and_names_the_peer_set_only():
    flat = _flat(_axis())
    assert "2. Industry overview" in flat and "contains no statistics" in flat
    assert "How the industry makes money" in flat and "Regulation" in flat
    assert "Market shares are not stored" in flat  # no share or size ranking is claimed


def test_the_industry_overview_depends_on_the_sector():
    bank = industry.overview("BANK", "private_sector_bank", "X Bank", ["A", "B"])
    it = industry.overview("IT_SERVICES", None, "X IT", [])
    assert "deposit" in bank["background"].lower() and "deposit" not in it["background"].lower()
    assert bank["industry_note"] and it["industry_note"] is None
    assert "empty" in it["fit"]["text"]  # no peers: said, not invented


# ---- growth comparison (CAGR, peer set, growth alpha) ---------------------------------------------


def _rev(session, c, values, basis="CONSOLIDATED", metric="INCOME.revenue_from_operations"):
    for end, v in zip(FY, values, strict=False):
        if v is not None:
            evidence(session, c, metric, v, end=end, ptype="ANNUAL", basis=basis)


def _stores(session, companies):
    names = {
        "INCOME.revenue_from_operations",
        "INCOME.total_income",
        "INCOME.net_profit",
        "TECH.return_1y",
        "PEERMEDIAN.TECH.return_1y",
    }
    return [(c, rd.load_store(session, c.id, names)) for c in companies]


def test_cagr_peer_cagr_and_growth_alpha_match_hand_calculation(seeded):  # noqa: F811
    own = mk_company(seeded, "Own", *IT)
    _rev(seeded, own, [100e9, None, None, 133.1e9])  # 3 years at 10% a year
    peers = []
    for i, end in enumerate((121e9, 133.1e9, 146.41e9)):  # 6.6%, 10.0%, 13.6% a year (approx)
        p = mk_company(seeded, f"Peer {i}", *IT)
        _rev(seeded, p, [100e9, None, None, end])
        peers.append(p)
    (_, own_store), *peer_stores = _stores(seeded, [own, *peers])
    g = industry.growth(own_store, peer_stores, "IT_SERVICES", "CONSOLIDATED")
    r = g["revenue"]
    assert r["peer_benchmark"] and r["company"]["years"] == 3 and r["company"]["cagr"] == "10"
    assert sorted(D(p["cagr"]) for p in r["peers"]) == [D("6.6"), D("10"), D("13.6")]
    assert r["peer_median_cagr"] == "10" and r["difference_vs_median_pp"] == "0"
    # alpha is the company CAGR minus the peer-set CAGR, in percentage points
    assert D(r["difference_vs_median_pp"]) == D(r["company"]["cagr"]) - D(r["peer_median_cagr"])
    assert r["company"]["evidence_ids"] and all(p["evidence_ids"] for p in r["peers"])


def test_growth_without_three_comparable_peers_is_reported_unavailable_not_estimated(seeded):  # noqa: F811
    own = mk_company(seeded, "Own2", *IT)
    _rev(seeded, own, [100e9, None, None, 150e9])
    peers = [mk_company(seeded, f"P{i}", *IT) for i in range(2)]
    for p in peers:
        _rev(seeded, p, [100e9, None, None, 120e9])
    (_, own_store), *peer_stores = _stores(seeded, [own, *peers])
    r = industry.growth(own_store, peer_stores, "IT_SERVICES", "CONSOLIDATED")["revenue"]
    assert (
        r["available"] and r["peer_benchmark"] is False and "fewer than 3 peers" in r["peer_reason"]
    )
    assert (
        "peers" not in r and r["company"]["cagr"] == "14.5"
    )  # company CAGR still shown, benchmark withheld


def test_peers_on_another_basis_or_missing_an_end_point_are_not_used(seeded):  # noqa: F811
    own = mk_company(seeded, "Own3", *IT)
    _rev(seeded, own, [100e9, None, None, 150e9])
    good = [mk_company(seeded, f"G{i}", *IT) for i in range(2)]
    for p in good:
        _rev(seeded, p, [100e9, None, None, 120e9])
    wrong_basis = mk_company(seeded, "WB", *IT)
    _rev(seeded, wrong_basis, [100e9, None, None, 120e9], basis="STANDALONE")
    (_, own_store), *peer_stores = _stores(seeded, [own, *good, wrong_basis])
    r = industry.growth(own_store, peer_stores, "IT_SERVICES", "CONSOLIDATED")["revenue"]
    assert r["peer_benchmark"] is False  # only 2 peers qualify on the same basis


def test_a_non_positive_start_value_gives_no_cagr(seeded):  # noqa: F811
    own = mk_company(seeded, "Loss", *IT)
    for end, v in zip(FY, [-5e9, None, None, 4e9], strict=False):
        if v is not None:
            evidence(
                seeded, own, "INCOME.net_profit", v, end=end, ptype="ANNUAL", basis="CONSOLIDATED"
            )
    ((_, store),) = _stores(seeded, [own])
    p = industry.growth(store, [], "IT_SERVICES", "CONSOLIDATED")["profit"]
    assert p["available"] is False and "undefined" in p["reason"]


def test_relative_price_return_is_the_difference_to_the_peer_median(seeded):  # noqa: F811
    own = mk_company(seeded, "Px", *IT)
    evidence(
        seeded,
        own,
        "TECH.return_1y",
        12.0,
        end=date(2026, 11, 6),
        ptype=None,
        basis=None,
        unit="percent",
        kind="CALCULATION",
    )
    evidence(
        seeded,
        own,
        "PEERMEDIAN.TECH.return_1y",
        5.0,
        end=date(2026, 11, 6),
        ptype=None,
        basis=None,
        unit="percent",
        kind="CALCULATION",
    )
    ((_, store),) = _stores(seeded, [own])
    pr = industry.growth(store, [], "IT_SERVICES", "CONSOLIDATED")["price_return"]
    assert pr[0]["period"] == "1Y" and pr[0]["difference_pp"] == "7"
    g = industry.growth(store, [], "IT_SERVICES", "CONSOLIDATED")
    assert any("not investment alpha" in d for d in g["definitions"])
    assert any("not risk-adjusted" in d and "not an index alpha" in d for d in g["definitions"])


def test_the_growth_section_discloses_definitions_and_the_peer_set_proxy():
    flat = _flat(_axis())
    assert "5. Growth versus the industry: CAGR and growth alpha" in flat
    assert "EquityOS holds no industry-wide statistics" in flat
    assert "CAGR = (ending value / starting value)" in flat and "not investment alpha" in flat
    assert "Relative price return (defined separately)" in flat


# ---- cyclicality ------------------------------------------------------------------------------------


def _row(key, label, values):
    return {
        "key": key,
        "label": label,
        "points": [
            {"value": str(v), "fy": f"FY{2023 + i}", "evidence_id": i + 1}
            for i, v in enumerate(values)
        ],
    }


def test_cyclicality_volatility_declines_and_recovery_match_hand_calculation():
    trend = {
        "annual": [
            _row("revenue", "Revenue", [100, 110, 99, 118.8]),
            _row("net_profit", "Net profit", [10, 12, 9, 13.5]),
        ]
    }
    c = cyclicality.build(trend, "AUTOMOBILES", {"revenue": {}}, [])
    rev = c["observed"]["revenue"]
    assert rev["growth"] == ["10.0", "-10.0", "20.0"] and rev["down_years"] == 1
    prof = c["observed"]["profit"]
    assert prof["growth"] == ["20.0", "-25.0", "50.0"]
    assert prof["volatility"] == "high" and D(prof["volatility_pp"]) > D("20")
    assert (
        prof["recoveries"][0]["fall_fy"] == "FY2025" and prof["recoveries"][0]["next_pct"] == "50.0"
    )
    assert prof["max_peak_to_trough_pct"] == "-25.0"
    assert (
        c["sector"]["classification"] == "cyclical"
        and c["observed"]["profit_to_revenue_volatility"]
    )


def test_cyclicality_separates_sector_template_from_observed_history_and_states_limits():
    trend = {
        "annual": [
            _row("revenue", "Revenue", [100, 108, 117]),
            _row("net_profit", "Net profit", [10, 10.8, 11.7]),
        ]
    }
    c = cyclicality.build(trend, "FMCG", {}, [])
    assert c["sector"]["classification"] == "defensive" and "template" in c["sector"]["note"]
    assert (
        c["observed"]["volatility_class"] == "low"
        and c["observed"]["versus_sector"] == "consistent with"
    )
    assert any("full cycle" in x for x in c["limits"]) and c["observed"]["n_years"] == 3
    assert c["sensitivities"] and "not how much it matters" in c["sensitivity_note"]


def test_cyclicality_with_no_history_makes_no_observed_claim():
    c = cyclicality.build({"annual": []}, "BANK", {}, [])
    assert c["observed"]["revenue"] is None and c["observed"]["volatility_class"] is None
    assert any("No annual revenue or profit history" in x for x in c["limits"])


def test_the_cyclicality_section_in_the_pdf():
    flat = _flat(_axis())
    assert "6. Business cyclicality" in flat and "Sector characterisation:" in flat
    assert "Downturn performance and recovery" in flat and "Limitations of this analysis" in flat
    assert "Indicators that could signal a downturn or recovery" in flat


# ---- company profile -------------------------------------------------------------------------------


def test_leaders_need_two_mentions_and_come_with_their_pages():
    page = "Amitabh Chaudhry Managing Director & CEO. Warm regards, Puneet Sharma Chief Financial Officer."
    out = profile.leaders(
        [(5, page), (9, "Amitabh Chaudhry Managing Director & Chief Executive Officer")]
    )
    assert [p["name"] for p in out] == [
        "Amitabh Chaudhry"
    ]  # Puneet Sharma appears once: not listed
    assert out[0]["titles"][0]["title"] == "Managing Director and CEO" and out[0]["titles"][0][
        "pages"
    ] == [5, 9]


def test_profile_extraction_is_verbatim_cited_and_filters_noise():
    page = (
        "Axis Bank Limited was incorporated in 1993 and provides a complete suite of banking services to customers. "
        "In Fiscal 2026 the Bank acquired 100% of the equity of Example Technologies Private Limited in India. "
        "The Sustainable Livelihood Program, our flagship initiative since 2011, impacted rural families. "
        "As on 31 March 2026 the Bank did not have any joint venture company at all in the year."
    )
    out = profile.extract([(12, page)])
    assert [i["page"] for i in out["history"]] == [12] and "incorporated in 1993" in out["history"][
        0
    ]["text"]
    assert any("acquired 100%" in i["text"] for i in out["strategic"])
    texts = [i["text"] for v in out.values() for i in v]
    assert all(t in page for t in texts)  # verbatim
    assert not any(
        "Livelihood" in t or "did not have" in t for t in texts
    )  # programmes and non-events skipped


def test_segments_give_shares_growth_and_margin_on_one_period_basis(seeded):  # noqa: F811
    c = mk_company(seeded, "SegCo", *IT)
    for seg, (a, b, res) in {"Alpha": (60e9, 72e9, 14.4e9), "Beta": (40e9, 48e9, None)}.items():
        evidence(
            seeded,
            c,
            "SEGMENT.segment_revenue",
            a,
            end=FY[2],
            ptype="ANNUAL",
            basis="CONSOLIDATED",
            dimension=seg,
        )
        evidence(
            seeded,
            c,
            "SEGMENT.segment_revenue",
            b,
            end=FY[3],
            ptype="ANNUAL",
            basis="CONSOLIDATED",
            dimension=seg,
        )
        if res:
            evidence(
                seeded,
                c,
                "SEGMENT.segment_result",
                res,
                end=FY[3],
                ptype="ANNUAL",
                basis="CONSOLIDATED",
                dimension=seg,
            )
    store = rd.load_store(seeded, c.id, profile.SEGMENT_NAMES)
    out = profile.segments(store, "CONSOLIDATED")
    rows = {r["segment"]: r for r in out["rows"]}
    assert out["available"] and out["period_end"] == "2026-03-31"
    assert rows["Alpha"]["share_pct"] == "60" and rows["Beta"]["share_pct"] == "40"  # 72 / 120
    assert rows["Alpha"]["growth_pct"] == "20" and rows["Alpha"]["result_margin_pct"] == "20"
    assert rows["Beta"]["result_margin_pct"] is None  # not stored: not computed


def test_a_single_segment_is_reported_as_unavailable(seeded):  # noqa: F811
    c = mk_company(seeded, "OneSeg", *BANK_SECTOR)
    store = rd.load_store(seeded, c.id, profile.SEGMENT_NAMES)
    out = profile.segments(store, "CONSOLIDATED")
    assert out["available"] is False and "no segment revenue" in out["reason"]


def test_company_without_an_annual_report_says_so(seeded):  # noqa: F811
    c = mk_company(seeded, "NoAR", *IT)
    n = profile.narrative(seeded, c.id)
    assert n["available"] is False and "no annual report" in n["reason"]
    g = profile.group_structure(seeded, c.id)
    assert g["available"] is False


def test_the_profile_section_discloses_what_is_not_held():
    flat = _flat(_axis())
    assert "3. Company and management profile" in flat
    assert "Leadership named in the annual report" in flat and "Amitabh Chaudhry" in flat
    assert "no biography is added" in flat
    assert (
        "Structured founder and management biographies" in flat
    )  # unavailable items are disclosed
    assert (
        "Business segments and revenue contribution" in flat
        and "no segment revenue by segment is stored" in flat
    )


# ---- graceful degradation and compatibility ---------------------------------------------------------


def test_a_report_without_the_new_analysis_still_renders_in_the_evidence_edition():
    c = _report()  # no analysis key
    flat = _flat(c)
    assert "EQUITY RESEARCH" in flat and "Industry overview" not in flat and "Contents" not in flat


def test_missing_data_sections_say_so_instead_of_inventing(seeded):  # noqa: F811
    c = _axis()
    a = c["analysis"]
    a["growth"]["revenue"] = {
        "available": False,
        "reason": "fewer than two annual values are stored for the company",
    }
    a["growth"]["profit"] = {"available": False, "reason": "x"}
    a["growth"]["price_return"] = []
    a["profile"]["narrative"] = {
        "available": False,
        "reason": "no annual report with stored page text is held for this company",
    }
    a["profile"]["segments"] = {
        "available": False,
        "reason": "no segment revenue by segment is stored",
    }
    a["cyclicality"]["observed"].update({"revenue": None, "profit": None, "volatility_class": None})
    flat = _flat(c)
    assert "fewer than two annual values" in flat and "No stored price-return evidence" in flat
    assert "no annual report with stored page text" in flat
    assert "Observed volatility cannot be measured" in flat
    assert not ISO.findall(flat)
