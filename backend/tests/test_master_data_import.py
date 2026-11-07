"""Tests for the exchange security-list importer.

All companies below are SYNTHETIC (invented names, ISINs with computed valid check digits) so the
tests can never be mistaken for real market data.
"""

import pytest
from sqlalchemy import func, select

from app.core.identifiers import is_valid_isin, isin_check_digit, issuer_key
from app.master_data.importer import SourceFile, derive_common_name, import_master_data
from app.master_data.parsers import (
    MasterFileError,
    parse_bse_scrip_list,
    parse_nse_equity_list,
)
from app.models import Company, CompanyAlias, MasterDataImport, Security, SecurityStatus
from app.seeds.loader import seed_reference_data, seed_test_universe
from app.services.company_resolver import ResolutionStatus, resolve_company


def make_isin(issuer4: str, serial: str = "0101", prefix: str = "INE") -> str:
    body = f"{prefix}{issuer4}{serial}"
    return body + isin_check_digit(body)


ALPHA = make_isin("AAA1")
BETA = make_isin("BBB2")
ALPHA_DVR = make_isin("AAA1", "0102", prefix="IN9")

NSE_HEADER = "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE"
BSE_HEADER = "Security Code,Issuer Name,Security Id,Security Name,Status,Group,Face Value,ISIN No,Industry,Instrument"


def nse_csv(*rows: str) -> str:
    return "\n".join([NSE_HEADER, *rows]) + "\n"


def bse_csv(*rows: str) -> str:
    return "\n".join([BSE_HEADER, *rows]) + "\n"


NSE_ALPHA = f"ALPHA,Alpha Industries Limited,EQ,08-SEP-1995,1,1,{ALPHA},1"
NSE_BETA = f"BETA,Beta Services Limited,EQ,01-JAN-2010,10,1,{BETA},10"
BSE_ALPHA = (
    f"590001,ALPHA INDUSTRIES LTD,ALPHA,ALPHA INDUSTRIES LTD,Active,A,1,{ALPHA},Chemicals,Equity"
)
BSE_BETA = (
    f"590002,BETA SERVICES LTD,BETA,BETA SERVICES LTD,Active,B,10,{BETA},IT - Software,Equity"
)


def run_import(session, nse: str | None = None, bse: str | None = None):
    sources = []
    if nse is not None:
        sources.append(SourceFile("NSE", "EQUITY_L.csv", nse.encode()))
    if bse is not None:
        sources.append(SourceFile("BSE", "bse_scrips.csv", bse.encode()))
    return import_master_data(session, sources)


@pytest.fixture
def base(session):
    seed_reference_data(session)  # exchanges and sectors only; no companies
    return session


# --- helpers ---------------------------------------------------------------------------------


def test_synthetic_isins_are_valid():
    assert all(is_valid_isin(i) for i in (ALPHA, BETA, ALPHA_DVR))


def test_issuer_key_merges_in9_prefix_with_ine():
    assert issuer_key(ALPHA) == issuer_key(ALPHA_DVR) == "INEAAA1"


@pytest.mark.parametrize(
    ("legal", "common"),
    [
        ("Reliance Industries Limited", "Reliance Industries"),
        ("HDFC BANK LTD", "HDFC BANK"),
        ("Foo Ltd.", "Foo"),
        ("Limited", "Limited"),
    ],
)
def test_derive_common_name(legal, common):
    assert derive_common_name(legal) == common


# --- parsers ---------------------------------------------------------------------------------


def test_nse_parser_reads_real_header_layout_and_skips_bad_rows():
    text = nse_csv(
        NSE_ALPHA,
        f"ALPHA,Alpha Dup Limited,EQ,08-SEP-1995,1,1,{BETA},1",  # duplicate symbol
        f"BADISIN,Bad Isin Limited,EQ,08-SEP-1995,1,1,{ALPHA[:-1]}{(int(ALPHA[-1]) + 1) % 10},1",
        f",No Symbol Limited,EQ,08-SEP-1995,1,1,{BETA},1",
        "NOISIN,No Isin Limited,EQ,08-SEP-1995,1,1,,1",
    )
    parsed = parse_nse_equity_list(text)
    assert parsed.total_rows == 5
    assert [r.symbol for r in parsed.rows] == ["ALPHA"]
    assert parsed.rows[0].listing_date.isoformat() == "1995-09-08"
    assert parsed.rows[0].series == "EQ"
    assert parsed.skipped == {
        "duplicate symbol in file": 1,
        "invalid ISIN": 2,
        "missing symbol": 1,
    }


def test_bse_parser_filters_instruments_status_and_codes():
    text = bse_csv(
        BSE_ALPHA,
        f"590003,Gamma Ltd,GAMMA,GAMMA LTD,Delisted,X,1,{make_isin('CCC3')},Textiles,Equity",
        f"590004,Fund Ltd,FUND,FUND LTD,Active,F,1,{make_isin('DDD4')},Funds,Mutual Fund",
        f"12AB,Badcode Ltd,BADC,BADC LTD,Active,A,1,{make_isin('EEE5')},Textiles,Equity",
        f"590006,Susp Ltd,SUSP,SUSP LTD,Suspended,Z,1,{make_isin('FFF6')},Textiles,Equity",
    )
    parsed = parse_bse_scrip_list(text)
    assert [r.symbol for r in parsed.rows] == ["ALPHA", "SUSP"]
    assert parsed.rows[1].status is SecurityStatus.SUSPENDED
    assert parsed.rows[0].industry == "Chemicals"
    assert parsed.skipped == {
        "unsupported status 'Delisted'": 1,
        "non-equity instrument": 1,
        "invalid BSE security code": 1,
    }


def test_missing_required_column_fails_loudly_instead_of_guessing():
    with pytest.raises(MasterFileError, match="Missing required column"):
        parse_nse_equity_list("SYMBOL,SOMETHING ELSE\nABC,xyz\n")


def test_empty_file_is_an_error():
    with pytest.raises(MasterFileError, match="empty"):
        parse_bse_scrip_list("")


def test_bom_and_windows_encoding_are_accepted(base):
    nse = ("﻿" + nse_csv(NSE_ALPHA)).encode("utf-8")
    report = import_master_data(base, [SourceFile("NSE", "EQUITY_L.csv", nse)])
    assert report.companies_created == 1
    legacy = nse_csv(f"CAFE,Caf\xe9 Foods Limited,EQ,08-SEP-1995,1,1,{BETA},1").encode("cp1252")
    assert import_master_data(base, [SourceFile("NSE", "x.csv", legacy)]).companies_created == 1


# --- import / merge --------------------------------------------------------------------------


def test_nse_and_bse_rows_merge_into_one_company_with_two_listings(base):
    report = run_import(base, nse_csv(NSE_ALPHA, NSE_BETA), bse_csv(BSE_ALPHA, BSE_BETA))
    assert report.companies_created == 2
    assert report.securities_created == 4
    company = base.scalar(select(Company).where(Company.issuer_key == "INEAAA1"))
    assert company.legal_name == "Alpha Industries Limited"  # NSE name wins (NSE imported first)
    assert company.common_name == "Alpha Industries"
    assert company.exchange_industry == "Chemicals"
    assert company.sector_id is None  # sector assignment is not an import concern (Phase 8)
    listings = {(s.exchange.code, s.symbol, s.security_code) for s in company.securities}
    assert listings == {("NSE", "ALPHA", None), ("BSE", "ALPHA", "590001")}
    assert "NSE EQUITY_L.csv" in company.master_data_source


def test_import_is_idempotent(base):
    run_import(base, nse_csv(NSE_ALPHA), bse_csv(BSE_ALPHA))
    companies = base.scalar(select(func.count()).select_from(Company))
    securities = base.scalar(select(func.count()).select_from(Security))
    aliases = base.scalar(select(func.count()).select_from(CompanyAlias))
    second = run_import(base, nse_csv(NSE_ALPHA), bse_csv(BSE_ALPHA))
    assert second.companies_created == 0 and second.securities_created == 0
    assert base.scalar(select(func.count()).select_from(Company)) == companies
    assert base.scalar(select(func.count()).select_from(Security)) == securities
    assert base.scalar(select(func.count()).select_from(CompanyAlias)) == aliases


def test_each_file_is_recorded_with_checksum_and_counts(base):
    bad_row = "BAD,Bad Limited,EQ,08-SEP-1995,1,1,INE000000000,1"
    run_import(base, nse_csv(NSE_ALPHA, bad_row))
    rec = base.scalar(select(MasterDataImport))
    assert (rec.exchange_code, rec.filename) == ("NSE", "EQUITY_L.csv")
    assert (rec.rows_total, rec.rows_imported, rec.rows_skipped) == (2, 1, 1)
    assert len(rec.sha256) == 64
    assert "invalid ISIN: 1" in rec.notes
    security = base.scalar(select(Security))
    assert security.last_import_id == rec.id


def test_dvr_security_attaches_to_the_same_company(base):
    dvr = f"ALPHADVR,Alpha Industries Limited,EQ,01-JAN-2012,1,1,{ALPHA_DVR},1"
    run_import(base, nse_csv(NSE_ALPHA, dvr))
    assert base.scalar(select(func.count()).select_from(Company)) == 1
    assert base.scalar(select(func.count()).select_from(Security)) == 2


def test_existing_company_is_matched_not_duplicated_and_name_not_overwritten(session):
    seed_reference_data(session)
    seed_test_universe(session)
    tcs_isin = "INE467B01029"
    nse = nse_csv(
        f"TCS,Tata Consultancy Services Ltd (renamed in file),EQ,25-AUG-2004,1,1,{tcs_isin},1"
    )
    report = run_import(session, nse)
    assert report.companies_created == 0
    assert report.companies_matched == 1
    assert session.scalar(select(func.count()).select_from(Company)) == 5
    tcs = session.scalar(select(Company).where(Company.issuer_key == "INE467B"))
    assert tcs.legal_name == "Tata Consultancy Services Limited"  # unchanged
    assert tcs.founding_year == 1968  # verified fields untouched
    assert any("renamed in file" in d for d in report.name_differences)
    # the exchange wording is kept as a searchable alias
    assert session.scalar(
        select(CompanyAlias).where(
            CompanyAlias.alias == "Tata Consultancy Services Ltd (renamed in file)"
        )
    )


def test_securities_missing_from_a_newer_file_are_reported_never_deleted(base):
    run_import(base, nse_csv(NSE_ALPHA, NSE_BETA))
    report = run_import(base, nse_csv(NSE_ALPHA))
    assert report.not_in_file == {"NSE": 1}
    assert base.scalar(select(func.count()).select_from(Security)) == 2


def test_bse_symbol_rename_keeps_the_same_security_row(base):
    run_import(base, None, bse_csv(BSE_ALPHA))
    renamed = BSE_ALPHA.replace(",ALPHA,", ",ALPHANEW,")
    report = run_import(base, None, bse_csv(renamed))
    assert report.securities_created == 0
    sec = base.scalar(select(Security))
    assert sec.symbol == "ALPHANEW" and sec.security_code == "590001"


def test_security_moving_to_a_different_issuer_is_reported(base):
    run_import(base, nse_csv(NSE_ALPHA))
    reassigned = f"ALPHA,Beta Services Limited,EQ,01-JAN-2010,10,1,{BETA},10"
    report = run_import(base, nse_csv(reassigned))
    assert report.securities_moved_between_companies == 1
    sec = base.scalar(select(Security))
    assert sec.isin == BETA
    assert sec.company.issuer_key == "INEBBB2"


def test_suspended_status_is_stored(base):
    susp = f"590009,Susp Ltd,SUSP,SUSP LTD,Suspended,Z,1,{make_isin('FFF6')},Textiles,Equity"
    run_import(base, None, bse_csv(susp))
    assert base.scalar(select(Security)).status is SecurityStatus.SUSPENDED


def test_unknown_exchange_is_rejected(base):
    with pytest.raises(MasterFileError, match="Unsupported exchange"):
        import_master_data(base, [SourceFile("NYSE", "x.csv", b"")])


# --- the whole point: any imported company becomes searchable --------------------------------


def test_imported_company_is_found_by_every_identifier_type(base):
    assert resolve_company(base, "ALPHA").status is ResolutionStatus.NOT_FOUND  # before import
    run_import(base, nse_csv(NSE_ALPHA), bse_csv(BSE_ALPHA))  # also invalidates the search cache
    for query in (ALPHA, "590001", "alpha", "Alpha Industries Ltd", "ALPHA INDUSTRIES"):
        res = resolve_company(base, query)
        assert res.status is ResolutionStatus.RESOLVED, query
        assert len(res.candidates) == 1
    fuzzy = resolve_company(base, "alfa industries")
    assert fuzzy.status is ResolutionStatus.SUGGESTIONS


def test_scale_import_of_five_thousand_companies_is_fast_and_searchable(base):
    import time

    rows_nse, rows_bse = [], []
    for n in range(5000):
        isin = make_isin(f"{n:04d}"[:4].translate(str.maketrans("0123456789", "ABCDEFGHIJ")))
        rows_nse.append(f"NS{n},Synthetic Company {n:04d} Limited,EQ,01-JAN-2020,1,1,{isin},1")
        rows_bse.append(
            f"{600000 + n},SYNTHETIC COMPANY {n:04d} LTD,BS{n},SYNTHETIC {n:04d},Active,B,1,{isin},Misc,Equity"
        )
    start = time.perf_counter()
    report = run_import(base, nse_csv(*rows_nse), bse_csv(*rows_bse))
    elapsed = time.perf_counter() - start
    assert report.companies_created == 5000 and report.securities_created == 10000
    assert elapsed < 60  # generous bound; guards against accidental O(n^2) behaviour

    started = time.perf_counter()
    res = resolve_company(base, "synthetic company 4999")
    assert res.status in (ResolutionStatus.RESOLVED, ResolutionStatus.SUGGESTIONS)
    assert time.perf_counter() - started < 5


# --- real-file edge cases found while dry-running the exchange downloads --------------------


def test_fund_and_etf_isins_are_not_companies():
    fund_isin = make_isin("GGG7", "0101").replace("INE", "INF")
    fund_isin = fund_isin[:-1] + isin_check_digit(fund_isin[:-1])
    text = bse_csv(
        BSE_ALPHA, f"590010,Some ETF,SOMEETF,SOME ETF,Active,F,10,{fund_isin},Funds,Equity"
    )
    parsed = parse_bse_scrip_list(text)
    assert [r.symbol for r in parsed.rows] == ["ALPHA"]
    (reason,) = [r for r in parsed.skipped if r.startswith("not a company security")]
    assert "INF" in reason
    assert "SOMEETF" in parsed.skipped_examples[reason][0]


def test_skipped_rows_keep_examples_for_the_report(base):
    placeholder = "590011,No Isin Ltd,NOISIN,NO ISIN LTD,Active,Y ,10.00,-,Equity"
    report = run_import(base, None, bse_csv(BSE_ALPHA, placeholder))
    text = report.summary()
    assert "invalid ISIN: 1" in text
    assert "NOISIN" in text


def test_rights_entitlement_never_names_the_company(base):
    rights_isin = make_isin("AAA1", "2001")  # security type 20, not 01
    rights = f"ALPHA-RE1,Alpha Industries Ltd-RE,BE,01-JAN-2024,1,1,{rights_isin},1"
    for rows in ((rights, NSE_ALPHA), (NSE_ALPHA, rights)):  # file order must not matter
        run_import(base, nse_csv(*rows))
        company = base.scalar(select(Company).where(Company.issuer_key == "INEAAA1"))
        assert company.legal_name == "Alpha Industries Limited"
        base.query(Security).delete()
        base.query(CompanyAlias).delete()
        base.query(Company).delete()
        base.flush()
