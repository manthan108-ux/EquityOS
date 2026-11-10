"""The offline XBRL reader and mapper: contexts, dimensions, units, families, basis, scale."""

from decimal import Decimal

import pytest

from app.docintel import xbrl, xbrl_map
from tests.xbrl_fixtures import (
    NS,
    UNITS,
    amount,
    bank_filing,
    duration,
    indas_filing,
    instant,
    text,
    wrap,
)

D = Decimal
EXPLICIT = '<xbrldi:explicitMember dimension="in-capmkt:SegmentsAxis">in-capmkt:RetailMember</xbrldi:explicitMember>'
TYPED = (
    '<xbrldi:typedMember dimension="in-capmkt:CompanyAxis"><in-capmkt:code>X1</in-capmkt:code>'
    "</xbrldi:typedMember>"
)


# ---- the file is untrusted ---------------------------------------------------------------------


def test_a_doctype_or_entity_declaration_is_refused_outright():
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">&b;</xbrli:xbrl>'
    )
    with pytest.raises(xbrl.XbrlError, match="DOCTYPE or ENTITY"):
        xbrl.parse(bomb)
    with pytest.raises(xbrl.XbrlError, match="DOCTYPE or ENTITY"):
        xbrl.parse(b'<!doctype html><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>')


def test_external_entities_are_never_resolved():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY f SYSTEM "file:///etc/passwd">]>'
        b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">&f;</xbrli:xbrl>'
    )
    with pytest.raises(xbrl.XbrlError):
        xbrl.parse(xxe)


@pytest.mark.parametrize(
    "body,message",
    [
        (b"", "not well-formed|empty"),
        (b"not xml at all", "not well-formed"),
        (b"<a><b></a>", "not well-formed"),
        (b"<html><body>hi</body></html>", "iXBRL"),
        (b'<report xmlns="urn:other"><x/></report>', "not an XBRL 2.1 instance"),
    ],
)
def test_files_that_are_not_an_xbrl_instance_are_refused_with_a_reason(body, message):
    with pytest.raises(xbrl.XbrlError, match=message):
        xbrl.parse(body)


def test_inline_xbrl_is_refused_not_half_read():
    ix = (
        b'<html xmlns="http://www.w3.org/1999/xhtml" '
        b'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
        b'<ix:nonFraction name="a:b" contextRef="c">1</ix:nonFraction></body></html>'
    )
    with pytest.raises(xbrl.XbrlError, match="inline XBRL"):
        xbrl.parse(ix)


def test_an_oversized_file_is_refused(monkeypatch):
    monkeypatch.setattr(xbrl, "MAX_BYTES", 100)
    with pytest.raises(xbrl.XbrlError, match="larger than"):
        xbrl.parse(b"<x>" + b"a" * 200 + b"</x>")


# ---- raw preservation --------------------------------------------------------------------------


def test_every_fact_context_unit_and_schema_reference_is_kept_as_filed():
    inst = xbrl.parse(indas_filing())
    assert inst.schema_refs == ["in-capmkt-2020-04-01.xsd"]
    assert set(inst.contexts) == {"cur", "pri", "end"}
    cur = inst.contexts["cur"]
    assert (str(cur.start), str(cur.end), cur.instant) == ("2026-07-01", "2026-09-30", None)
    assert inst.contexts["end"].is_instant and str(inst.contexts["end"].period_end) == "2026-09-30"
    assert inst.contexts["cur"].entity_id == "RELIANCE"
    rev = next(
        f for f in inst.facts if f.local == "RevenueFromOperations" and f.context_ref == "cur"
    )
    assert rev.text == "1000" and rev.decimals == "2" and rev.unit_ref == "INR"
    assert rev.qname == "in-capmkt:RevenueFromOperations" and rev.numeric == D(1000)
    assert inst.unit(rev).label == "INR"
    assert inst.units["INRPS"].label == "INR/shares"
    assert inst.problems == []


def test_explicit_and_typed_dimensions_are_preserved_on_the_context():
    inst = xbrl.parse(
        wrap(
            duration("seg", "2026-07-01", "2026-09-30", dims=EXPLICIT),
            duration("typ", "2026-07-01", "2026-09-30", dims=TYPED),
            UNITS,
            amount("RevenueFromOperations", "seg", 400),
            amount("RevenueFromOperations", "typ", 100),
        )
    )
    assert inst.contexts["seg"].dimensions == (
        ("in-capmkt:SegmentsAxis", "in-capmkt:RetailMember", "explicit"),
    )
    assert inst.contexts["typ"].dimensions == (("in-capmkt:CompanyAxis", "X1", "typed"),)
    assert len(inst.facts) == 2


def test_nil_text_and_undefined_references_are_recorded_not_hidden():
    body = wrap(
        duration("cur", "2026-07-01", "2026-09-30"),
        UNITS,
        '<in-capmkt:OtherIncome contextRef="cur" unitRef="INR" xsi:nil="true"/>',
        text("NameOfTheCompany", "cur", "Example Limited"),
        amount("Income", "ghost", 5),
        '<in-capmkt:Expenses contextRef="cur" unitRef="NOPE" decimals="0">5</in-capmkt:Expenses>',
    )
    inst = xbrl.parse(body)
    nil = next(f for f in inst.facts if f.local == "OtherIncome")
    assert nil.nil and nil.numeric is None
    name = next(f for f in inst.facts if f.local == "NameOfTheCompany")
    assert name.text == "Example Limited" and name.numeric is None
    assert any("context ghost is not defined" in p for p in inst.problems)
    assert any("unit NOPE is not defined" in p for p in inst.problems)


def test_a_context_defined_twice_and_a_bad_date_are_reported():
    body = wrap(
        duration("c", "2026-07-01", "2026-09-30"),
        duration("c", "2026-07-01", "2026-09-30"),
        duration("bad", "2026-13-01", "2026-09-30"),
        UNITS,
    )
    inst = xbrl.parse(body)
    assert any("defined twice" in p for p in inst.problems)
    assert any("not a date" in p for p in inst.problems)


# ---- taxonomy families are separate ------------------------------------------------------------


def test_the_family_is_read_from_the_concepts_present():
    assert xbrl_map.detect_family(xbrl.parse(indas_filing()))[0] == xbrl_map.IND_AS
    assert xbrl_map.detect_family(xbrl.parse(bank_filing()))[0] == xbrl_map.BANK


def test_a_filing_mixing_both_families_is_ambiguous_and_maps_nothing():
    body = indas_filing(extra=amount("InterestEarned", "cur", 1, prefix="in-bse-fin"))
    mapping = xbrl_map.build(xbrl.parse(body))
    assert mapping.family is None and mapping.blocks == [] and mapping.mapped == {}
    assert any(
        i["code"] == "TAXONOMY_FAMILY" and "ambiguous" in i["detail"] for i in mapping.issues
    )


def test_a_bank_concept_in_an_ind_as_filing_is_not_mapped_from_the_other_table():
    # 'Deposits' is a banking concept; an Ind AS filing that carries it must not map it
    inst = xbrl.parse(indas_filing(extra=amount("Deposits", "end", 7)))
    mapping = xbrl_map.build(inst, family=xbrl_map.IND_AS)
    assert "in-capmkt:Deposits" in mapping.unmapped
    items = {k for _s, k in mapping.mapped.values()}
    assert "deposits" not in items and "revenue_from_operations" in items


def test_ind_as_income_items_are_not_taken_from_the_banking_table():
    mapping = xbrl_map.build(xbrl.parse(bank_filing()))
    items = {k for _s, k in mapping.mapped.values()}
    assert {"interest_earned", "interest_expended", "advances", "deposits"} <= items
    assert "revenue_from_operations" not in items


def test_the_family_can_be_stated_by_the_supplier_and_a_wrong_one_maps_little():
    mapping = xbrl_map.build(xbrl.parse(bank_filing()), family=xbrl_map.IND_AS)
    items = {k for _s, k in mapping.mapped.values()}
    assert "interest_earned" not in items  # not in the Ind AS table
    with pytest.raises(ValueError):
        xbrl_map.build(xbrl.parse(bank_filing()), family="MADE_UP")


# ---- standalone / consolidated -----------------------------------------------------------------


@pytest.mark.parametrize(
    "stated,expected", [("Consolidated", "CONSOLIDATED"), ("Standalone", "STANDALONE")]
)
def test_the_basis_is_the_one_the_filing_states(stated, expected):
    mapping = xbrl_map.build(xbrl.parse(indas_filing(basis=stated)))
    assert mapping.basis == expected and mapping.basis_source == "stated in the filing"
    assert {b.basis for b in mapping.blocks} == {expected}


def test_a_filing_that_does_not_state_its_basis_has_none_and_is_never_guessed():
    mapping = xbrl_map.build(xbrl.parse(indas_filing(basis=None)))
    assert mapping.basis is None
    assert {b.basis for b in mapping.blocks} == {None}
    assert any(i["code"] == "BASIS_NOT_STATED" for i in mapping.issues)


def test_a_supplied_basis_fills_a_gap_but_never_overrides_the_filing():
    filled = xbrl_map.build(xbrl.parse(indas_filing(basis=None)), basis="STANDALONE")
    assert filled.basis == "STANDALONE" and "supplied" in filled.basis_source
    clash = xbrl_map.build(xbrl.parse(indas_filing(basis="Consolidated")), basis="STANDALONE")
    assert clash.basis is None and "conflict" in clash.basis_source


def test_a_dimension_member_naming_the_basis_is_a_stated_basis():
    member = (
        '<xbrldi:explicitMember dimension="in-capmkt:ConsolidatedAndStandaloneAxis">'
        "in-capmkt:StandaloneMember</xbrldi:explicitMember>"
    )
    body = wrap(
        duration("cur", "2026-07-01", "2026-09-30"),
        duration("dim", "2026-07-01", "2026-09-30", dims=member),
        UNITS,
        text("LevelOfRoundingUsedInFinancialStatements", "cur", "Lakhs"),
        amount("RevenueFromOperations", "cur", 5),
    )
    assert xbrl_map.detect_basis(xbrl.parse(body)) == ("STANDALONE", "stated in the filing")


# ---- contexts: periods and dimensions ----------------------------------------------------------


def months_of(mapping, statement, header_part):
    block = next(b for b in mapping.blocks if b.statement == statement)
    return next(c.period.duration_months for c in block.columns if header_part in c.header)


def test_duration_contexts_become_quarter_and_year_columns_and_instants_point_in_time():
    mapping = xbrl_map.build(xbrl.parse(indas_filing()))
    inc = next(b for b in mapping.blocks if b.statement == "INCOME")
    assert [c.period.duration_months for c in inc.columns] == [3, 3]
    assert [str(c.period.period_end) for c in inc.columns] == ["2026-09-30", "2025-09-30"]
    bal = next(b for b in mapping.blocks if b.statement == "BALANCE_SHEET")
    assert bal.columns[0].period.point_in_time and bal.columns[0].period.duration_months is None


def test_half_year_and_annual_periods_are_recognised_and_odd_lengths_are_flagged():
    body = wrap(
        duration("h1", "2026-04-01", "2026-09-30"),
        duration("fy", "2025-04-01", "2026-03-31"),
        duration("odd", "2026-05-01", "2026-09-30"),
        UNITS,
        text("LevelOfRoundingUsedInFinancialStatements", "h1", "Lakhs"),
        text("NatureOfReportStandaloneConsolidated", "h1", "Standalone"),
        amount("RevenueFromOperations", "h1", 10),
        amount("RevenueFromOperations", "fy", 20),
        amount("RevenueFromOperations", "odd", 30),
    )
    block = next(b for b in xbrl_map.build(xbrl.parse(body)).blocks if b.statement == "INCOME")
    by_header = {c.header: c for c in block.columns}
    assert by_header["2026-04-01 to 2026-09-30"].period.duration_months == 6
    assert by_header["2025-04-01 to 2026-03-31"].period.duration_months == 12
    odd = by_header["2026-05-01 to 2026-09-30"]
    assert odd.problem and "not a quarter" in odd.problem and not odd.usable


def test_dimensional_facts_stay_raw_and_do_not_feed_the_primary_statements():
    body = indas_filing(extra=amount("RevenueFromOperations", "seg", 400)).replace(
        b"</xbrli:xbrl>",
        duration("seg", "2026-07-01", "2026-09-30", dims=EXPLICIT).encode() + b"</xbrli:xbrl>",
    )
    inst = xbrl.parse(body)
    mapping = xbrl_map.build(inst)
    assert mapping.dimensional == 1
    seg_fact = next(f for f in inst.facts if f.context_ref == "seg")
    assert seg_fact.index not in mapping.mapped
    inc = next(b for b in mapping.blocks if b.statement == "INCOME")
    assert D(400) not in {f.value for f in inc.facts}


def test_an_instant_context_on_a_flow_item_and_a_duration_on_a_balance_item_are_left_raw():
    body = wrap(
        duration("d", "2026-07-01", "2026-09-30"),
        instant("i", "2026-09-30"),
        UNITS,
        text("LevelOfRoundingUsedInFinancialStatements", "d", "Lakhs"),
        text("NatureOfReportStandaloneConsolidated", "d", "Standalone"),
        amount("RevenueFromOperations", "i", 10),  # a flow item at an instant
        amount("Assets", "d", 10),  # a point-in-time item over a duration
        amount("OtherIncome", "d", 1),
    )
    mapping = xbrl_map.build(xbrl.parse(body))
    assert sum(1 for i in mapping.issues if i["code"] == "PERIOD_KIND") == 2
    assert {k for _s, k in mapping.mapped.values()} == {"other_income"}


def test_two_different_values_for_one_item_and_period_are_marked_duplicate():
    body = indas_filing(extra=amount("OtherIncome", "cur", 51))
    mapping = xbrl_map.build(xbrl.parse(body))
    inc = next(b for b in mapping.blocks if b.statement == "INCOME")
    other = [f for f in inc.facts if f.key == "other_income"]
    assert any(f.duplicate for f in other)


# ---- units and scale ---------------------------------------------------------------------------


def test_the_level_of_rounding_gives_the_unit_and_a_missing_one_gives_none():
    assert xbrl_map.build(xbrl.parse(indas_filing(rounding="Lakhs"))).unit == "lakhs"
    assert xbrl_map.build(xbrl.parse(indas_filing(rounding="Crores"))).unit == "crores"
    none = xbrl_map.build(xbrl.parse(indas_filing(rounding=None)))
    assert none.unit is None and "no level of rounding" in none.unit_source


def test_decimals_that_contradict_the_stated_rounding_make_the_scale_ambiguous():
    # "Lakhs" but every amount says decimals=-5: absolute rupees rounded to lakhs look like this
    mapping = xbrl_map.build(xbrl.parse(indas_filing(rounding="Lakhs", decimals="-5")))
    assert mapping.unit is None
    assert any(i["code"] == "SCALE_AMBIGUOUS" for i in mapping.issues)


def test_a_supplied_scale_resolves_the_ambiguity_but_cannot_contradict_the_filing():
    ok = xbrl_map.build(xbrl.parse(indas_filing(rounding=None)), amount_scale="crores")
    assert ok.unit == "crores" and "supplied" in ok.unit_source
    clash = xbrl_map.build(xbrl.parse(indas_filing(rounding="Lakhs")), amount_scale="crores")
    assert clash.unit is None and any(i["code"] == "SCALE_CONFLICT" for i in clash.issues)
    with pytest.raises(ValueError):
        xbrl_map.build(xbrl.parse(indas_filing()), amount_scale="gazillions")


# ---- ratios ------------------------------------------------------------------------------------


def ratio_facts(mapping):
    block = next(b for b in mapping.blocks if b.statement == "RATIOS")
    return {f.key: f.value for f in block.facts}


def test_percent_ratios_are_kept_as_filed():
    r = ratio_facts(xbrl_map.build(xbrl.parse(bank_filing(ratios="percent"))))
    assert r == {
        "gross_npa_ratio": D("1.16"),
        "net_npa_ratio": D("0.35"),
        "capital_adequacy_ratio": D("16.27"),
    }


def test_a_pure_ratio_is_refused_unless_the_supplier_says_what_it_means():
    mapping = xbrl_map.build(xbrl.parse(bank_filing(ratios="pure")))
    assert set(ratio_facts(mapping).values()) == {None}
    assert sum(1 for i in mapping.issues if i["code"] == "RATIO_REFUSED") == 3
    frac = xbrl_map.build(xbrl.parse(bank_filing(ratios="pure")), pure_means_fraction=True)
    assert ratio_facts(frac)["gross_npa_ratio"] == D("116.00")
    asis = xbrl_map.build(xbrl.parse(bank_filing(ratios="pure")), pure_means_fraction=False)
    assert ratio_facts(asis)["gross_npa_ratio"] == D("1.16")


def test_a_zero_ratio_is_a_placeholder_and_is_never_read_as_reported():
    body = bank_filing().replace(
        b'decimals="2">1.16</in-bse-fin:PercentageOfGrossNpa>',
        b'decimals="2">0.00</in-bse-fin:PercentageOfGrossNpa>',
    )
    mapping = xbrl_map.build(xbrl.parse(body))
    assert ratio_facts(mapping)["gross_npa_ratio"] is None
    assert any("placeholder" in i["detail"] for i in mapping.issues)


# ---- identity ----------------------------------------------------------------------------------


def test_entity_identifiers_are_collected_from_the_contexts():
    assert xbrl_map.build(xbrl.parse(indas_filing(ident="HDFCBANK"))).entity_ids == ["HDFCBANK"]


def test_the_namespace_constant_is_well_formed():
    assert "xbrldi" in NS
