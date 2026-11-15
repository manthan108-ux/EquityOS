from datetime import date
from decimal import Decimal as D

import pytest

from app.research import assumptions as asm
from app.research import valuation_setup as vs
from tests.test_sector import mk_company, seeded  # noqa: F401


def test_beta_uses_the_industry_when_it_has_enough_firms_else_the_all_market_beta():
    beta, industry, why = vs.beta_for("TCS")
    assert (beta, industry) == (D("0.864"), "Software (System & Application)")
    assert "82 firms" in why and "not this company's own" in why
    # Integrated oil and gas has a single Indian firm: the all-market beta is used and the rationale says why
    beta, industry, why = vs.beta_for("RELIANCE")
    assert industry == "Total Market (without financials)" and beta == D("0.785")
    assert "only 1" in why


def test_a_symbol_with_no_industry_gets_no_beta():
    with pytest.raises(asm.AssumptionError):
        vs.beta_for("NOSUCH")


def test_every_pilot_symbol_is_mapped_and_every_source_is_dated():
    data = vs.load()
    from app.pipeline.cli import _pilot_symbols

    for sym in _pilot_symbols():
        vs.beta_for(sym, data)
    for k in ("risk_free_rate", "equity_risk_premium", "tax_rate"):
        assert data[k]["source"] and data[k]["source_url"] and date.fromisoformat(data[k]["as_of"])
    assert date.fromisoformat(data["beta"]["as_of"])


def obs(vals):
    return [(date(2020 + i, 3, 31), D(str(v)), [i]) for i, v in enumerate(vals)]


def test_scenarios_use_the_last_three_for_base_and_the_range_for_bear_and_bull():
    sc = vs._scenarios(obs([10, 12, 20, 14, 16]), higher_is_better=True)
    assert (sc["bear"], sc["base"], sc["bull"]) == (D("10.00"), D("16.67"), D("20.00"))
    cost = vs._scenarios(obs([10, 12, 20, 14, 16]), higher_is_better=False)
    assert (cost["bear"], cost["bull"]) == (
        D("20.00"),
        D("10.00"),
    )  # a higher cost is the worse case
    assert sc["n"] == 5 and sc["years"] == "2020-2024"


def test_fewer_than_three_observations_produce_nothing():
    assert vs._scenarios(obs([10, 12]), higher_is_better=True) is None


def test_the_market_inputs_are_entered_as_sourced_and_reentering_changes_nothing(seeded):  # noqa: F811
    session, company = seeded, mk_company(seeded, "Setup Co")
    out = vs.apply_market(session, company, "TCS", today=date(2026, 11, 15))
    assert any(x.startswith("beta 0.864") for x in out) and any(
        x.startswith("tax_rate") for x in out
    )
    inputs = asm.current_inputs(session, company.id, today=date(2026, 11, 15))
    rf = inputs["VALUATION_INPUT.risk_free_rate"]
    assert rf["class"] == "SOURCED_INPUT" and rf["value"] == "5.44" and not rf["stale"]
    first = rf["evidence_id"]
    vs.apply_market(session, company, "TCS", today=date(2026, 11, 15))
    again = asm.current_inputs(session, company.id, today=date(2026, 11, 15))
    assert again["VALUATION_INPUT.risk_free_rate"]["evidence_id"] == first
