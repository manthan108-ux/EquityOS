"""The analytical layer of the research report (phases 2-7): trends, peers, valuation, commentary,
thesis, sector profiles, and the full PDF built from them. Evidence is synthetic; the arithmetic is
checked by hand."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal as D

from app.models import Peer, PeerSet
from app.research import analysis as an
from app.research import mgmt, thesis, valuation
from app.research import readiness as rd
from app.research import report as rp
from app.research.pdf_pro import render
from app.research.sectors import BANK, IT_SERVICES, SECTORS, sector_for
from tests.research_helpers import evidence, mk_company
from tests.test_research_pdf import BANK as BANK_SECTOR
from tests.test_research_pdf import IT, pdf_text
from tests.test_sector import seeded  # noqa: F401

FY = [date(2023, 3, 31), date(2024, 3, 31), date(2025, 3, 31), date(2026, 3, 31)]
Q = date(2026, 9, 30)
NOW = datetime(2026, 11, 8, tzinfo=UTC)


def add(
    session,
    company,
    metric,
    values,
    *,
    ends=FY,
    ptype="ANNUAL",
    basis="CONSOLIDATED",
    unit="inr",
    kind="FINANCIAL_STATEMENT",
):
    for end, v in zip(ends, values, strict=False):
        if v is not None:
            evidence(
                session, company, metric, v, end=end, ptype=ptype, basis=basis, unit=unit, kind=kind
            )


def peerset(session, company, peers):
    ps = PeerSet(
        company_id=company.id, built_at=NOW, as_of=date(2026, 11, 8), sector_code="x",
        criteria={}, candidates=len(peers), peer_count=len(peers), thin=len(peers) < 3,
    )  # fmt: skip
    session.add(ps)
    session.flush()
    for i, p in enumerate(peers, 1):
        session.add(
            Peer(
                peer_set_id=ps.id, company_id=p.id, tier="SAME_INDUSTRY", rank=i, reason=f"peer {i}"
            )
        )
    session.flush()


def store(session, company, sector):
    names = {n for m in (*sector.trend, *sector.quarterly, *sector.peer) for n in m.names}
    names |= {"PRICE.close", "VALUATION.pe_annual", "VALUATION.price_to_book", "INCOME.eps_basic"}
    return rd.load_store(session, company.id, names)


# ---- trends ---------------------------------------------------------------------------------------


def test_growth_is_computed_from_stored_values_and_flagged_when_unusual(seeded):  # noqa: F811
    c = mk_company(seeded, "Trend Co", *IT)
    add(seeded, c, "INCOME.revenue_from_operations", [100e9, 110e9, 121e9, 200e9])
    add(seeded, c, "INCOME.net_profit", [10e9, 10e9, 9e9, 9e9])
    t = an.trends(store(seeded, c, IT_SERVICES), IT_SERVICES, "CONSOLIDATED")
    rev = next(r for r in t["annual"] if r["key"] == "revenue")
    assert [rev["changes"][0]["value"], rev["changes"][1]["value"]] == ["10", "10"]
    assert D(rev["changes"][2]["value"]).quantize(D("0.01")) == D("65.29")  # 200 / 121 - 1
    assert any(u["key"] == "revenue" and u["period"] == "FY2026" for u in t["unusual"])  # >25%
    # profit 10 -> 9 is -10%; 9 -> 9 is stable
    prof = next(r for r in t["annual"] if r["key"] == "net_profit")
    assert D(prof["changes"][1]["value"]) == D("-10")
    assert an.direction(prof["changes"][2]) == "stable"
    assert all(o["kind"] == "DERIVED" and o["evidence_ids"] for o in t["observations"])


def test_a_non_positive_base_gives_no_percentage(seeded):  # noqa: F811
    c = mk_company(seeded, "Loss Co", *IT)
    add(seeded, c, "INCOME.net_profit", [-5e9, 4e9])
    t = an.trends(store(seeded, c, IT_SERVICES), IT_SERVICES, "CONSOLIDATED")
    row = next(r for r in t["annual"] if r["key"] == "net_profit")
    assert row["changes"] == []  # a percentage change from a loss is meaningless


def test_bases_are_never_mixed_in_one_series(seeded):  # noqa: F811
    c = mk_company(seeded, "Basis Co", *IT)
    add(seeded, c, "INCOME.net_profit", [10e9], ends=[FY[3]], basis="CONSOLIDATED")
    add(seeded, c, "INCOME.net_profit", [7e9, 8e9, 9e9, 9.5e9], basis="STANDALONE")
    t = an.trends(store(seeded, c, IT_SERVICES), IT_SERVICES, "CONSOLIDATED")
    row = next(r for r in t["annual"] if r["key"] == "net_profit")
    assert {p["basis"] for p in row["points"]} == {"STANDALONE"}  # whole series on one basis
    assert "standalone basis" in row["basis_note"] and len(row["points"]) == 4


def test_quarterly_yoy_and_qoq_only_when_the_earlier_quarter_exists(seeded):  # noqa: F811
    c = mk_company(seeded, "Qtr Co", *IT)
    ends = [date(2025, 9, 30), date(2025, 12, 31), date(2026, 6, 30), Q]
    add(seeded, c, "INCOME.net_profit", [100, 105, 110, 120], ends=ends, ptype="QUARTERLY")
    t = an.trends(store(seeded, c, IT_SERVICES), IT_SERVICES, "CONSOLIDATED")
    row = next(r for r in t["quarterly"] if r["key"] == "net_profit")
    assert D(row["yoy"]["value"]) == D("20")  # 120 vs 100, same quarter a year earlier
    assert row["qoq"] is not None and D(row["qoq"]["value"]).quantize(D("0.1")) == D(
        "9.1"
    )  # 120 / 110


def test_banks_get_no_cash_flow_flags(seeded):  # noqa: F811
    c = mk_company(seeded, "Bank CF", *BANK_SECTOR)
    add(seeded, c, "CASH_FLOW.summary.operating", [100, -50, 500, -300])
    t = an.trends(store(seeded, c, BANK), BANK, "CONSOLIDATED")
    assert not t["unusual"] and not any(r["key"] == "cfo" for r in t["annual"])
    assert not BANK.cash_flow_meaningful


# ---- peers ----------------------------------------------------------------------------------------


def _bank_with_peers(session, own_roe, peer_roes, *, peer_basis="CONSOLIDATED", peer_end=FY[3]):
    c = mk_company(session, "Own Bank", *BANK_SECTOR)
    add(
        session,
        c,
        "RETURN.roe_closing",
        [own_roe],
        ends=[FY[3]],
        unit="percent",
        kind="CALCULATION",
    )
    peers = []
    for i, v in enumerate(peer_roes):
        p = mk_company(session, f"Peer Bank {i}", *BANK_SECTOR)
        add(
            session,
            p,
            "RETURN.roe_closing",
            [v],
            ends=[peer_end],
            unit="percent",
            basis=peer_basis,
            kind="CALCULATION",
        )
        peers.append(p)
    peerset(session, c, peers)
    return c, peers


def _peer_row(session, c, key="roe"):
    a, *_ = an.peer_analysis(
        session, c, lambda cid, names: rd.load_store(session, cid, set(names)), BANK, "CONSOLIDATED"
    )
    return next((r for r in a["rows"] if r["key"] == key), None), a


def test_peer_rank_median_and_position_with_three_comparable_peers(seeded):  # noqa: F811
    c, _ = _bank_with_peers(seeded, 12, [10, 14, 16])
    r, a = _peer_row(seeded, c)
    assert r["n_comparable"] == 3 and D(r["median"]) == D("14")
    assert (r["rank"], r["of"], r["position"]) == (3, 4, "worse")  # higher ROE is better
    assert a["summary"]["worse"] == ["Return on equity"]


def test_fewer_than_three_comparable_peers_gives_no_median_or_rank(seeded):  # noqa: F811
    c, _ = _bank_with_peers(seeded, 12, [10, 14])
    r, _ = _peer_row(seeded, c)
    assert (
        r["n_comparable"] == 2
        and "median" not in r
        and "rank" not in r
        and "fewer than 3" in r["note"]
    )


def test_a_peer_on_a_different_period_or_basis_is_shown_but_never_compared(seeded):  # noqa: F811
    c, _ = _bank_with_peers(seeded, 12, [10, 14, 16], peer_end=FY[2])
    r, _ = _peer_row(seeded, c)
    assert r["n_comparable"] == 0 and all("different period" in p["reason"] for p in r["peers"])
    assert all(p["value"] for p in r["peers"])  # still shown for reference
    c2, _ = _bank_with_peers(seeded, 12, [10, 14, 16], peer_basis="STANDALONE")
    r2, _ = _peer_row(seeded, c2)
    assert r2["n_comparable"] == 0  # standalone peers are not compared with a consolidated figure


def test_a_metric_is_compared_on_the_basis_more_peers_report(seeded):  # noqa: F811
    c = mk_company(seeded, "Mixed Bank", *BANK_SECTOR)
    for basis in ("CONSOLIDATED", "STANDALONE"):
        add(
            seeded,
            c,
            "RETURN.roe_closing",
            [12],
            ends=[FY[3]],
            unit="percent",
            basis=basis,
            kind="CALCULATION",
        )
    peers = []
    for i, v in enumerate([10, 14, 16]):
        p = mk_company(seeded, f"MB peer {i}", *BANK_SECTOR)
        add(
            seeded,
            p,
            "RETURN.roe_closing",
            [v],
            ends=[FY[3]],
            unit="percent",
            basis="STANDALONE",
            kind="CALCULATION",
        )
        peers.append(p)
    peerset(seeded, c, peers)
    r, _ = _peer_row(seeded, c)
    assert (
        r["basis"] == "STANDALONE"
        and r["n_comparable"] == 3
        and "standalone basis" in r["basis_note"]
    )


def test_lower_is_better_metrics_rank_the_other_way(seeded):  # noqa: F811
    c = mk_company(seeded, "NPA Bank", *BANK_SECTOR)
    add(
        seeded,
        c,
        "RATIOS.gross_npa_ratio",
        [1.0],
        ends=[Q],
        ptype="QUARTERLY",
        basis=None,
        unit="percent",
    )
    peers = []
    for i, v in enumerate([1.5, 2.0, 3.0]):
        p = mk_company(seeded, f"NPA peer {i}", *BANK_SECTOR)
        add(
            seeded,
            p,
            "RATIOS.gross_npa_ratio",
            [v],
            ends=[Q],
            ptype="QUARTERLY",
            basis=None,
            unit="percent",
        )
        peers.append(p)
    peerset(seeded, c, peers)
    r, _ = _peer_row(seeded, c, "gnpa")
    assert (r["rank"], r["position"]) == (1, "better")


# ---- valuation ------------------------------------------------------------------------------------


def test_justified_pb_and_market_implied_roe_match_hand_calculation():
    assert valuation._justified_pb(D("12"), D("12"), D("6")) == D("1")  # (12-6)/(12-6)
    assert valuation._justified_pb(D("15"), D("12"), D("6")) == D("1.5")
    assert (
        valuation._justified_pb(D("12"), D("6"), D("6")) is None
    )  # cost of equity must exceed growth
    assert valuation._implied_roe(D("1.5"), D("12"), D("6")) == D("15")  # inverse of the above


def test_bank_scenarios_use_stated_assumptions_and_the_pb_basis(seeded):  # noqa: F811
    c = mk_company(seeded, "Val Bank", *BANK_SECTOR)
    add(seeded, c, "RETURN.roe_closing", [12], ends=[FY[3]], unit="percent", kind="CALCULATION")
    add(
        seeded,
        c,
        "PRICE.close",
        [1000],
        ends=[date(2026, 11, 6)],
        ptype=None,
        basis=None,
        unit="inr_per_share",
        kind="PRICE",
    )
    add(
        seeded,
        c,
        "VALUATION.price_to_book",
        [2],
        ends=[date(2026, 11, 6)],
        ptype="QUARTERLY",
        unit="x",
        kind="CALCULATION",
    )
    s = store(seeded, c, BANK)
    price = valuation._latest(s, ("PRICE.close",))
    pb = valuation._latest(s, ("VALUATION.price_to_book",))
    out = valuation.bank_intrinsic(s, BANK, "CONSOLIDATED", price, pb, None)
    assert out["completed"] and out["inputs"]["book_value_per_share"] == "500"  # 1000 / 2
    base = out["scenarios"]["base"]
    assert base["justified_pb"] == "1" and base["value_per_share"] == "500"  # (12-6)/(12-6) x 500
    assert out["scenarios"]["bear"]["assumptions"]["cost_of_equity"] == "13"
    assert D(
        next(r for r in out["market_implied"]["rows"] if r["cost_of_equity"] == "12")["implied_roe"]
    ) == D("18")  # 6 + 2 x 6
    assert "not target prices" in valuation.ASSUMPTION_NOTE
    # an ROE on the other basis is never used with this P/B
    c2 = mk_company(seeded, "Val Bank 2", *BANK_SECTOR)
    add(
        seeded,
        c2,
        "RETURN.roe_closing",
        [12],
        ends=[FY[3]],
        unit="percent",
        basis="STANDALONE",
        kind="CALCULATION",
    )
    add(
        seeded,
        c2,
        "PRICE.close",
        [1000],
        ends=[date(2026, 11, 6)],
        ptype=None,
        basis=None,
        unit="inr_per_share",
        kind="PRICE",
    )
    add(
        seeded,
        c2,
        "VALUATION.price_to_book",
        [2],
        ends=[date(2026, 11, 6)],
        ptype="QUARTERLY",
        unit="x",
        kind="CALCULATION",
    )
    s2 = store(seeded, c2, BANK)
    out2 = valuation.bank_intrinsic(
        s2,
        BANK,
        "CONSOLIDATED",
        valuation._latest(s2, ("PRICE.close",)),
        valuation._latest(s2, ("VALUATION.price_to_book",)),
        None,
    )
    assert out2["completed"] is False and "consolidated basis" in out2["reason"].lower()


def test_relative_scenarios_need_three_peers_and_scale_the_price():
    class R:
        def __init__(self, v):
            self.value = D(v)
            self.id = 1
            self.unit = "x"
            self.period_end = date(2026, 11, 6)
            self.basis = None

    price, pe = R("100"), R("20")
    row = {"peers": [{"comparable": True, "value": v} for v in ("10", "20", "30")]}
    out = valuation.relative_scenarios(price, pe, row)
    assert out["completed"]
    assert out["scenarios"]["base"]["value_per_share"] == "100"  # 100 x 20 / 20
    assert out["scenarios"]["bear"]["value_per_share"] == "50"  # lowest peer P/E 10
    assert out["scenarios"]["bull"]["value_per_share"] == "150"
    few = valuation.relative_scenarios(price, pe, {"peers": row["peers"][:2]})
    assert few["completed"] is False and "at least 3" in few["reason"]


# ---- management commentary ------------------------------------------------------------------------


def test_commentary_is_extractive_and_filters_furniture():
    page = (
        "The Bank continues to sharpen its strategic focus on deposit premiumisation across customer segments. "
        "Going forward, the Bank will continue to focus on disciplined growth and deeper customer engagement. "
        "Pursuant to Regulation 17 of the SEBI Listing Regulations the Bank has constituted committees of the Board. "
        "Rs 1,234 5,678 9,012 3,456 7,890 1,234 5,678 9,012 3,456 7,890 1,234 5,678 9,012 3,456. "
        "Short one."
    )
    out = mgmt.extract([(7, page)])
    texts = [i["text"] for items in out.values() for i in items]
    assert any("strategic focus" in t for t in texts)
    assert any(t.startswith("Going forward") for t in texts)
    assert not any("Regulation 17" in t for t in texts)  # governance furniture
    assert not any("1,234" in t for t in texts)  # a table row
    assert all(i["page"] == 7 for items in out.values() for i in items)
    assert all(t in page for t in texts)  # verbatim, never paraphrased


def test_commentary_states_why_it_is_unavailable(seeded):  # noqa: F811
    c = mk_company(seeded, "No MDA", *IT)
    out = mgmt.commentary(seeded, c.id)
    assert out["available"] is False and "no management-discussion section" in out["reason"]


# ---- thesis ---------------------------------------------------------------------------------------


def _analysis_stub(**over):
    base = {
        "trend": {"annual": [], "quarterly": [], "unusual": [], "observations": []},
        "peers": {"peers": [{"name": "a"}] * 4, "rows": [], "unavailable_metrics": ["Net NPA ratio"],
                  "summary": {"ranked_metrics": 0, "better": [], "worse": [], "in_line": []}},
        "valuation": {"snapshot": {"pe": None, "pb": None, "price": None}, "peer": {"rows": []},
                      "relative": {"completed": False}, "intrinsic": {"completed": False}, "not_completed": ["x"]},
        "management": {"available": False},
    }  # fmt: skip
    base.update(over)
    return base


def _row(key, label, pts, better="higher", kind="amount"):
    changes = []
    for a, b in zip(pts, pts[1:], strict=False):
        changes.append(
            an.change({"value": str(a), "fy": "FY0"}, {"value": str(b), "fy": "FY1"}, kind)
        )
    return {"key": key, "label": label, "better": better, "kind": kind, "changes": changes,
            "points": [{"value": str(v), "evidence_id": i + 1, "period_end": "2026-03-31", "unit": "inr"} for i, v in enumerate(pts)]}  # fmt: skip


def test_trajectory_rules_and_no_recommendation():
    up = _analysis_stub(
        trend={
            "annual": [
                _row("net_profit", "Net profit", [100, 120]),
                _row("total_income", "Total income", [100, 110]),
            ],
            "quarterly": [],
            "unusual": [],
            "observations": [],
        }
    )
    assert thesis.trajectory(up)["label"] == "improving"
    down = _analysis_stub(
        trend={
            "annual": [_row("net_profit", "Net profit", [100, 80]), _row("eps", "EPS", [10, 8])],
            "quarterly": [],
            "unusual": [],
            "observations": [],
        }
    )
    assert thesis.trajectory(down)["label"] == "deteriorating"
    flat = _analysis_stub(
        trend={
            "annual": [_row("net_profit", "Net profit", [100, 100.2])],
            "quarterly": [],
            "unusual": [],
            "observations": [],
        }
    )
    assert thesis.trajectory(flat)["label"] == "stable"
    assert thesis.trajectory(_analysis_stub())["label"] == "cannot be established"
    t = thesis.build(
        up,
        IT_SERVICES,
        score={"available": False},
        missing=[],
        mgmt={"available": False},
        applied=[],
        auditor_flags=None,
    )
    assert t["conclusion"]["recommendation"].startswith("see the verdict")
    qs = [a["question"] for a in t["conclusion"]["answers"]]
    assert len(qs) == 9 and "Is profitability sustainable?" in qs


def test_thesis_points_carry_evidence_and_lower_is_better_is_respected():
    a = _analysis_stub(
        trend={
            "annual": [_row("cost_to_income", "Cost-to-income ratio", [50, 46], "lower", "pct")],
            "quarterly": [],
            "unusual": [],
            "observations": [],
        }
    )
    t = thesis.build(
        a,
        BANK,
        score={"available": False},
        missing=[],
        mgmt={"available": False},
        applied=[],
        auditor_flags=None,
    )
    assert any("Cost-to-income" in p["text"] for p in t["bull"])  # a fall is good
    assert all(p["evidence_ids"] for p in t["bull"])
    assert all(
        p["kind"] in ("INTERPRETATION", "ANALYTICAL HYPOTHESIS") for p in t["bull"] + t["bear"]
    )


def test_sector_risks_are_labelled_as_templates_not_evidence():
    t = thesis.build(
        _analysis_stub(),
        BANK,
        score={"available": False},
        missing=[],
        mgmt={"available": False},
        applied=[],
        auditor_flags=None,
    )
    assert t["risks"]["sector"] and all(
        "SECTOR TEMPLATE" in r["kind"] for r in t["risks"]["sector"]
    )
    assert t["risks"]["governance"][0]["text"].startswith("No auditor-report qualification")
    flagged = thesis.build(
        _analysis_stub(),
        BANK,
        score={"available": False},
        missing=[],
        mgmt={"available": False},
        applied=[],
        auditor_flags=["Key Audit Matters"],
    )
    assert "Key Audit Matters" in flagged["risks"]["governance"][0]["text"]


# ---- sector profiles (phase 7) --------------------------------------------------------------------


def test_every_profile_has_its_own_lens_metrics_and_risks():
    for code in ("BANK", "IT_SERVICES", "FMCG", "AUTOMOBILES", "ENERGY"):
        s = SECTORS[code]
        assert s.lens and s.trend and s.peer and s.monitor and s.risks
    assert {m.key for m in BANK.peer} >= {
        "nim",
        "gnpa",
        "nnpa",
        "car",
        "roe",
        "roa",
        "cost_to_income",
        "pb",
        "pe",
    }
    assert "nim" not in {m.key for m in IT_SERVICES.peer} and "ebit_margin" in {
        m.key for m in IT_SERVICES.peer
    }
    assert "cfo" not in {m.key for m in BANK.trend} and "cfo" in {m.key for m in IT_SERVICES.trend}
    assert BANK.valuation == "BANK" and IT_SERVICES.valuation == "EARNINGS"
    assert sector_for("UNKNOWN").code == "GENERIC_NONFIN"
    assert BANK.risks != IT_SERVICES.risks


# ---- the report and the PDF -----------------------------------------------------------------------


def test_the_report_content_carries_a_serialisable_analysis(seeded):  # noqa: F811
    from tests.research_helpers import complete_company
    from tests.test_research_pdf import make_report

    c = complete_company(seeded, "Full Bank", *BANK_SECTOR)
    report = make_report(seeded, c, rp.COMPLETE)
    a = report.content["analysis"]
    assert a["available"] and a["sector"]["code"] == "BANK"
    json.dumps(report.content)  # the whole content (with the analysis) is JSON
    for key in ("trend", "peers", "valuation", "management", "thesis"):
        assert key in a
    assert a["verdict"]["verdict"] in ("BUY", "SELL", "WATCH")
    assert a["thesis"]["conclusion"]["recommendation"].startswith("see the verdict")


def test_an_analysis_failure_never_blocks_the_report(seeded, monkeypatch):  # noqa: F811
    from tests.research_helpers import complete_company
    from tests.test_research_pdf import make_report

    def boom(*a, **k):
        raise RuntimeError("analysis exploded")

    monkeypatch.setattr(rp.insight, "build", boom)
    c = complete_company(seeded, "Resilient", *IT)
    report = make_report(seeded, c, rp.COMPLETE)
    assert report.content["analysis"]["available"] is False
    assert "RuntimeError" in report.content["analysis"]["reason"]
    text, _ = pdf_text(
        render(
            report.content,
            version=1,
            mode=report.mode,
            report_id=str(report.id),
            content_hash=report.content_hash,
        )
    )
    assert "EQUITY RESEARCH" in text  # falls back to the evidence edition


def test_the_full_axis_pdf_has_every_section_and_no_recommendation():
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / "axis_report_v2.json"
    c = json.loads(path.read_text(encoding="utf-8"))
    data = render(
        c,
        version=2,
        mode="COMPLETE",
        report_id="3134f0a2-479d-42f3-bd40-3e6ffbf50ee0",
        content_hash="c" * 64,
    )
    text, pages = pdf_text(data)
    flat = text.replace("\n", " ")
    for h in ("Contents", "1. Executive summary", "2. Industry overview", "3. Company and management profile",
              "4. Financial performance", "5. Growth versus the industry", "6. Business cyclicality",
              "7. Peer benchmarking", "8. Management commentary", "9. Valuation analysis",
              "10. Investment thesis", "11. Financial scorecard", "12. Research conclusion",
              "Appendix A - Source register", "Appendix D - Analytical methodology",
              "Appendix E - Data quality"):  # fmt: skip
        assert h in flat, h
    assert pages >= 15
    assert (
        "Bull case" in flat
        and "Bear case" in flat
        and "Key catalysts" in flat
        and "Monitoring indicators" in flat
    )
    assert "illustrative" in flat and "sector template" in flat.lower()  # qualifications in words
    assert "MANAGEMENT STATEMENT" not in flat and "SECTOR TEMPLATE" not in flat  # no inline labels
    assert "EquityOS verdict" in flat and "investment advice" in flat
    assert "WATCH" in flat and "Evidence confidence" in flat
    assert (
        flat.count("not part of this edition") <= 2
    )  # business-model engine; forward-looking evidence
    assert "None" not in text.replace("None established", "").replace("None.", "")
    # the figures printed are the stored ones
    assert "16.27%" in flat and "1,62,212" in flat
    for word in ("strong buy", "outperform", "price target of"):
        assert word not in flat.lower()
