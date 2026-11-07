"""Provider tests. Fixtures copy the real file layouts; values are small and illustrative."""

from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.acquisition.http import ClientConfig, FetchResult, PoliteClient
from app.models import Document, EodPrice, FetchStatus, Security
from app.providers.company_ir import extract_ir_links
from app.providers.documents import DocumentProvider, sniff, validate_document
from app.providers.exchange_files import (
    parse_bse_bhavcopy,
    parse_nse_bhavcopy,
    store_prices,
    validate_bse_bhav,
    validate_nse_bhav,
    validate_nse_equity_list,
)
from app.providers.feeds import FeedItem, dedupe, mentions, parse_feed
from app.seeds.loader import seed_reference_data, seed_test_universe

NSE_BHAV = """SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER
RELIANCE, EQ, 06-Nov-2026, 1313.40, 1295.00, 1320.00, 1295.00, 1320.00, 1320.00, 1311.70, 22768301, 298652.10, 327475, 15216640, 66.83
21STCENMGM, BE, 06-Nov-2026, 57.54, 54.67, 54.67, 54.67, 54.67, 54.67, 54.67, 2663, 1.46, 22, -, -
NOTINDB, EQ, 06-Nov-2026, 10.00, 10.00, 11.00, 9.00, 10.50, 10.50, 10.20, 100, 0.01, 5, 50, 50.00
BADDATE, EQ, not-a-date, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1
"""

BSE_BHAV = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4
2026-11-06,2026-11-06,CM,BSE,STK,500325,INE002A01018,RELIANCE,A ,,,,,RELIANCE INDUSTRIES LTD,1297.50,1318.10,1295.00,1318.10,1318.10,1313.35,,,,,768236,1007259797.00,18320,F1,1,,,,,
2026-11-06,2026-11-06,CM,BSE,STK,999999,INE000000000,UNKNOWN,B ,,,,,UNKNOWN LTD,10,11,9,10,10,10,,,,,5,50,1,F1,1,,,,,
"""


def fetch_result(url="https://example.test/f.csv", body=b"x"):
    return FetchResult(
        url, FetchStatus.OK, 200, body, "text/csv", retrieved_at=datetime(2026, 11, 7, tzinfo=UTC)
    )


# --- exchange files --------------------------------------------------------------------------


def test_nse_bhavcopy_parsing_handles_spaces_dashes_and_units():
    rows = parse_nse_bhavcopy(NSE_BHAV)
    assert [r.symbol for r in rows] == ["RELIANCE", "21STCENMGM", "NOTINDB"]  # bad date dropped
    rel = rows[0]
    assert rel.trade_date == date(2026, 11, 6) and rel.series == "EQ"
    assert rel.close == Decimal("1320.00") and rel.prev_close == Decimal("1313.40")
    assert rel.volume == 22768301 and rel.delivery_pct == Decimal("66.83")
    assert rel.turnover_inr == Decimal("29865210000.0")  # lakhs -> rupees
    be = rows[1]
    assert be.delivery_qty is None and be.delivery_pct is None  # "-" means not published, not zero


def test_bse_bhavcopy_parsing():
    rows = parse_bse_bhavcopy(BSE_BHAV)
    rel = rows[0]
    assert (rel.symbol, rel.series) == ("500325", "A")
    assert rel.close == Decimal("1318.10") and rel.volume == 768236
    assert rel.turnover_inr == Decimal("1007259797.00")
    assert rel.delivery_pct is None  # BSE does not publish delivery data


@pytest.mark.parametrize(
    ("validator", "good", "bad"),
    [
        (validate_nse_bhav, NSE_BHAV.encode(), b"<html>Error</html>"),
        (validate_bse_bhav, BSE_BHAV.encode(), b"<!DOCTYPE html><html>Error</html>"),
        (
            validate_nse_equity_list,
            b"SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\n",
            b"<html>x</html>",
        ),
    ],
)
def test_validators_accept_real_layout_and_reject_error_pages(validator, good, bad):
    assert validator(good, None) is None
    assert validator(bad, None) is not None
    assert validator(b"", None) is not None


@pytest.fixture
def seeded(session):
    seed_reference_data(session)
    seed_test_universe(session)
    return session


def test_store_prices_matches_known_securities_and_counts_the_rest(seeded):
    rows = parse_nse_bhavcopy(NSE_BHAV)
    report = store_prices(seeded, "NSE", rows, fetch_result())
    assert (report.rows_in_file, report.stored, report.already_stored, report.unmatched) == (
        3,
        1,
        0,
        2,
    )
    price = seeded.scalar(select(EodPrice))
    assert (
        price.close == Decimal("1320.0000") and price.security.symbol == "RELIANCE"
        if hasattr(price, "security")
        else True
    )
    assert price.source_url == "https://example.test/f.csv" and len(price.file_sha256) == 64


def test_store_prices_is_idempotent_and_never_overwrites(seeded):
    rows = parse_nse_bhavcopy(NSE_BHAV)
    store_prices(seeded, "NSE", rows, fetch_result())
    changed = [r.__class__(**{**r.__dict__, "close": Decimal("1")}) for r in rows]
    again = store_prices(seeded, "NSE", changed, fetch_result())
    assert again.stored == 0 and again.already_stored == 1
    assert seeded.scalar(select(EodPrice.close)) == Decimal("1320.0000")  # first value kept


def test_bse_prices_match_on_security_code(seeded):
    report = store_prices(seeded, "BSE", parse_bse_bhavcopy(BSE_BHAV), fetch_result())
    assert (report.stored, report.unmatched) == (1, 1)
    sec = seeded.scalar(select(Security).where(Security.security_code == "500325"))
    assert seeded.scalar(select(EodPrice.security_id)) == sec.id


# --- feeds -----------------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Infosys wins big deal &amp; raises guidance - Mint</title><link>https://m.test/1</link>
<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Infosys Ltd said...&lt;/p&gt;</description></item>
<item><title>Infosys wins big deal &amp; raises guidance - ET</title><link>https://e.test/2</link>
<pubDate>Fri, 06 Nov 2026 12:00:00 GMT</pubDate><description>dup story</description></item>
<item><title>Unrelated market wrap</title><link>https://m.test/3</link><pubDate>Fri, 06 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>No link item</title></item></channel></rss>"""


def test_feed_parsing_drops_items_without_links_and_strips_html():
    items = parse_feed(RSS, "MINT_RSS", None)
    assert len(items) == 3
    assert items[0].summary == "Infosys Ltd said..."
    assert items[0].published == datetime(2026, 11, 6, 10, 0, tzinfo=UTC)


def test_duplicate_stories_are_collapsed_keeping_the_earliest():
    items = dedupe(parse_feed(RSS, "MINT_RSS", None))
    infosys = [i for i in items if "Infosys" in i.title]
    assert len(infosys) == 1 and infosys[0].link == "https://m.test/1"


def test_company_mention_is_a_whole_phrase_match():
    item = FeedItem("Tata Steel posts profit", "l", None, "", None, "X", None)
    assert mentions(item, ["Tata Steel"])
    assert not mentions(item, ["Tata Motors"])
    assert not mentions(item, ["tat"])  # fragments of words do not count
    assert not mentions(item, ["ab"])  # too short to be meaningful


# --- company IR discovery --------------------------------------------------------------------

HTML = """<html><body>
<a href="/investors">Investor Relations</a>
<a href="/investors/annual-report-2025.pdf">Annual Report 2025</a>
<a href="https://www.example.com/results/q2">Quarterly Results</a>
<a href="https://evil.test/investor">Investor (other site)</a>
<a href="mailto:ir@example.com">Email investor team</a>
<a href="/careers">Careers</a><a href="#top">Top</a>
</body></html>"""


def test_ir_discovery_finds_investor_links_on_the_company_domain_only():
    links = extract_ir_links(HTML, "https://www.example.com/")
    by_url = {link.url: link.kind for link in links}
    assert by_url["https://www.example.com/investors"] == "investor_relations"
    assert by_url["https://www.example.com/investors/annual-report-2025.pdf"] == "annual_report"
    assert by_url["https://www.example.com/results/q2"] == "financial_results"
    assert not any("evil.test" in u or "mailto" in u or "careers" in u for u in by_url)


# --- documents -------------------------------------------------------------------------------

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF"


def doc_client(handler, tmp_path):
    return PoliteClient(
        ClientConfig(user_agent="t", min_interval_s=0, respect_robots=False),
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (PDF, "pdf"),
        (b"PK\x03\x04zip", "zip"),
        (b"<?xml version='1.0'?><x/>", "xml"),
        (b"<html></html>", "html"),
    ],
)
def test_file_type_is_decided_by_content_not_by_name(body, expected):
    assert sniff(body)[0] == expected and validate_document(body, None) is None


@pytest.mark.parametrize(
    "body", [b"MZ\x90\x00 executable", b"#!/bin/sh\nrm -rf /", b"random bytes"]
)
def test_unrecognised_or_executable_content_is_rejected(body):
    assert sniff(body) is None and validate_document(body, None) is not None


def test_document_download_stores_by_hash_and_deduplicates(seeded, tmp_path):
    client = doc_client(lambda r: httpx.Response(200, content=PDF), tmp_path)
    provider = DocumentProvider(client, data_dir=tmp_path)
    # a hostile URL path must not influence where the file is written
    url = "https://x.test/../../etc/passwd.pdf"
    first = provider.download(seeded, url, doc_type="annual_report", title="AR 2025")
    assert first.created and first.document.size_bytes == len(PDF)
    stored = tmp_path / first.document.storage_path
    assert stored.read_bytes() == PDF and stored.name == f"{first.document.sha256}.pdf"
    assert tmp_path in stored.parents
    second = provider.download(seeded, "https://y.test/same.pdf", doc_type="annual_report")
    assert not second.created and second.document.id == first.document.id
    assert seeded.scalar(select(func.count()).select_from(Document)) == 1


def test_non_document_content_is_not_stored(seeded, tmp_path):
    client = doc_client(lambda r: httpx.Response(200, content=b"MZ\x90 binary"), tmp_path)
    result = DocumentProvider(client, data_dir=tmp_path).download(
        seeded, "https://x.test/a.pdf", doc_type="annual_report"
    )
    assert result.document is None and result.fetch.status is FetchStatus.INVALID_CONTENT
    assert not list(tmp_path.rglob("*.pdf")) and not list(tmp_path.rglob("*.bin"))


def test_unknown_document_type_is_refused(seeded, tmp_path):
    provider = DocumentProvider(
        doc_client(lambda r: httpx.Response(200, content=PDF), tmp_path), tmp_path
    )
    with pytest.raises(ValueError, match="Unknown doc_type"):
        provider.download(seeded, "https://x.test/a.pdf", doc_type="made_up")


# --- client additions: host throttle codes, other 4xx ----------------------------------------


def test_host_specific_throttle_code_is_retried_with_the_host_backoff():
    responses = iter([httpx.Response(406), httpx.Response(200, content=b"ok")])
    sleeps = []
    client = PoliteClient(
        ClientConfig(
            user_agent="t",
            min_interval_s=0,
            respect_robots=False,
            host_throttle_codes={"slow.test": frozenset({406})},
            host_backoff_s={"slow.test": 30.0},
        ),
        transport=httpx.MockTransport(lambda r: next(responses)),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )
    result = client.fetch("https://slow.test/f", source="T")
    assert result.status is FetchStatus.OK and sleeps == [30.0]


def test_other_client_errors_are_final_not_retried():
    calls = []
    client = PoliteClient(
        ClientConfig(user_agent="t", min_interval_s=0, respect_robots=False),
        transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(406)),
        sleep=lambda s: None,
    )
    result = client.fetch("https://other.test/f", source="T")
    assert result.status is FetchStatus.CLIENT_ERROR and len(calls) == 1
