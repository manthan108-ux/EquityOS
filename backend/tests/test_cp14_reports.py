"""CP14: gap consolidation, empty table rows/columns, historical-multiple scenarios, HOLD on the cover,
the plain-language needs line, and the pilot validation checks."""

from decimal import Decimal as D
from types import SimpleNamespace

from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, Spacer

from app.research import pdf_gaps
from app.research import pdf_pro as pp
from app.research import relative as rel
from app.research import validate_pilot as vp
from app.research.pdf_analysis import _needs

ST = {n: ParagraphStyle(n) for n in ("h1", "h2", "body", "small", "bullet")}


def text_of(flowables):
    return " | ".join(f.getPlainText() for f in flowables if isinstance(f, Paragraph))


def test_gap_sentences_move_to_one_appendix_under_their_section():
    story = [
        Paragraph("Cash flow", ST["h1"]),
        Paragraph("Operating cash flow was 100.", ST["body"]),
        Paragraph("Free cash flow is not stored for this company.", ST["small"]),
        Paragraph("Valuation", ST["h1"]),
        Paragraph(
            "Free cash flow is not stored for this company.", ST["small"]
        ),  # repeated: stated once
        Paragraph("Price is 10.", ST["body"]),
    ]
    out = pdf_gaps.consolidate(story, ST)
    body, appendix = text_of(out).split("Appendix F")
    assert "Operating cash flow was 100." in body and "Price is 10." in body
    assert "not stored" not in body
    assert appendix.count("Free cash flow is not stored") == 1
    assert "Cash flow" in appendix  # grouped under the heading it was under


def test_a_heading_left_empty_is_dropped_and_tagged_gap_bullets_move():
    gap = Paragraph("fewer than two annual values are stored", ST["bullet"])
    gap._gap = True
    story = [Paragraph("Section", ST["h1"]), Paragraph("Empty part", ST["h2"]), gap, PageBreak(), Paragraph("Next", ST["h1"])]  # fmt: skip
    out = pdf_gaps.consolidate(story, ST)
    names = [f.getPlainText() for f in out if isinstance(f, Paragraph)]
    assert "Empty part" not in names[: names.index("Next")]
    assert any("fewer than two annual values" in n for n in names[names.index("Next") :])


def test_no_gaps_means_no_appendix():
    out = pdf_gaps.consolidate([Paragraph("A", ST["h1"]), Paragraph("Plain text.", ST["body"])], ST)
    assert "Appendix F" not in text_of(out)


def p(s):
    return Paragraph(s, ST["body"])


def test_all_missing_rows_and_columns_are_left_out_of_a_table_and_listed():
    pp.PRUNED.clear()
    rows = [
        [p("Measure"), p("FY2024"), p("FY2025"), p("Capex")],
        [p("Income"), p(""), p(""), p("")],  # a sub-heading row is not "missing"
        [p("Revenue"), p("100"), p("110"), p("n/a")],
        [p("Buybacks"), p("n/a"), p("-"), p("n/a")],
    ]
    t = pp._table(rows, [60, 30, 30, 30])
    assert len(t._cellvalues) == 3  # header, sub-heading, revenue
    assert len(t._cellvalues[0]) == 3  # the Capex column went
    assert sorted(pp.PRUNED) == ["Buybacks (no stored value)", "Capex (no stored value)"]
    t2 = pp._table(rows[:1] + [[p("Revenue"), p("1"), p("2"), p("3")]] * 2, [60, 30, 30, 30])
    assert len(t2._cellvalues[0]) == 4  # nothing missing: untouched


def test_a_style_command_past_a_pruned_table_does_not_break_it():
    from reportlab.platypus import TableStyle

    pp.PRUNED.clear()
    rows = [[p("a"), p("b"), p("c")], [p("x"), p("n/a"), p("n/a")], [p("y"), p("1"), p("n/a")]]
    t = pp._table(rows, [50, 30, 30])
    t.setStyle(TableStyle([("BACKGROUND", (0, 5), (-1, 5), "red"), ("BACKGROUND", (0, 0), (-1, 0), "blue")]))  # fmt: skip


def test_needs_line_says_each_missing_input_once_without_the_scenario_name():
    sc = {
        "bear": {
            "completed": False,
            "missing": [
                "long-run growth for the bear scenario",
                "cost of equity for the bear scenario (or an entered discount rate)",
            ],
        },  # fmt: skip
        "base": {
            "completed": False,
            "missing": [
                "long-run growth for the base scenario",
                "cost of equity for the base scenario (or an entered discount rate)",
            ],
        },  # fmt: skip
    }
    n = _needs(sc)
    assert n.count("long-run growth") == 1 and "scenario" not in n and "entered discount" not in n


def test_the_cover_colours_a_hold():
    assert "HOLD" in pp.VERDICT_COLOUR


# ---------------------------------------------------------------- historical-multiple scenarios


def rec(i, v, end=None):
    return SimpleNamespace(id=i, value=D(str(v)), period_end=end)


def hist(pes):
    return {"rows": [{"pe": str(x), "eps_evidence_id": 100 + k} for k, x in enumerate(pes)]}


def test_historical_scenarios_apply_the_companys_own_pe_range_to_current_eps():
    price, own = rec(1, 100), rec(2, 20)  # EPS = 5
    out = rel.historical_scenarios(price, own, hist([10, 20, 30]))
    assert out["completed"] and out["in_verdict"] is False
    v = {k: s["value_per_share"] for k, s in out["scenarios"].items()}
    assert (v["bear"], v["base"], v["bull"]) == ("50", "100", "150")  # EPS 5 x 10 / 20 / 30
    assert out["scenarios"]["base"]["evidence_ids"] == [1, 2, 100, 101, 102]
    assert out["sensitivity"]["grid"] and "year-end" in out["spread"]


def test_historical_scenarios_need_three_years_and_say_so():
    out = rel.historical_scenarios(rec(1, 100), rec(2, 20), hist([10, 20]))
    assert out["completed"] is False and "year-end P/E observation" in out["reason"]
    assert rel.historical_scenarios(None, rec(2, 20), hist([1, 2, 3]))["completed"] is False


# ---------------------------------------------------------------- validation checks


def test_validation_flags_a_median_from_too_few_peers_and_a_rank_without_four():
    a = {"peers": {"rows": [{"label": "ROE", "median": "10", "rank": 2, "of": 3, "peers": [{"value": "9", "comparable": True}, {"value": "11", "comparable": True}]}]}}  # fmt: skip
    out = vp.peer_checks(a)
    assert any("median is shown from 2" in x for x in out) and any("fewer than 4" in x for x in out)


def test_validation_flags_unordered_scenarios_and_untraceable_points():
    a = {"valuation": {"relative": {"completed": True, "scenarios": {"bear": {"value_per_share": "120", "evidence_ids": [1]}, "base": {"value_per_share": "100", "evidence_ids": [1]}, "bull": {"value_per_share": "130", "evidence_ids": [1]}}}}}  # fmt: skip
    assert any("not ordered" in x for x in vp.valuation_checks(a))
    a2 = {"verdict": {"net_points": 5, "verdict": "BUY", "levels": {}, "components": [{"points": 1, "weight": 2, "available": True}]},
          "thesis": {"bull": [{"text": "a claim with no evidence", "kind": "INTERPRETATION", "evidence_ids": []}], "bear": [], "catalysts": [], "risk_register": {}}}  # fmt: skip
    out = vp.conclusion_checks(a2)
    assert any("a BUY/SELL without a target" in x for x in out)
    assert any("untraceable point" in x for x in out)
    assert any("net points 5" in x for x in out)


def test_the_gap_spacer_flowable_is_not_mistaken_for_content():
    out = pdf_gaps.consolidate(
        [Paragraph("H", ST["h1"]), Paragraph("Sub", ST["h2"]), Spacer(1, 4)], ST
    )
    assert "Sub" not in text_of(out)


# ---------------------------------------------------------------- stale reference price


def test_a_reference_price_older_than_the_latest_close_is_flagged_with_its_lag():
    from datetime import date

    from app.research import verdict as vd

    today = date(2026, 11, 16)  # Monday: the latest expected close is Friday 13 November
    fresh = vd._price_freshness("2026-11-13", today)
    assert fresh["price_stale"] is False and fresh["price_lag_days"] == 0
    old = vd._price_freshness("2026-11-06", today)
    assert old["price_stale"] is True and old["price_lag_days"] == 5
    assert old["price_expected_date"] == "2026-11-13"
    assert vd._price_freshness(None, today)["price_stale"] is None


def test_validation_flags_growth_at_or_above_the_cost_of_equity():
    sc = {c: {"value_per_share": v, "evidence_ids": [1], "assumptions": {"growth": g, "cost_of_equity": "10"}} for c, v, g in (("bear", "50", "4"), ("base", "60", "6"), ("bull", "70", "10"))}  # fmt: skip
    out = vp.valuation_checks({"valuation": {"intrinsic": {"completed": True, "scenarios": sc}}})
    assert out == ["intrinsic/bull: long-run growth 10% is not below the cost of equity 10%"]
