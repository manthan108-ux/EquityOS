"""Enrichment: a model may only extract; every quote must be verbatim in the page text."""

# ruff: noqa: F811
import pytest
from sqlalchemy import select

from app.acquisition.http import FetchResult, FetchStatus
from app.models import Company, CompanyEnrichment
from app.research import enrich
from app.seeds.loader import seed_reference_data, seed_test_universe

HOME = (
    "<html><head><title>Example Ltd</title></head><body><nav>menu</nav>"
    '<a href="/about-us">About us</a><a href="/leadership">Our leadership</a>'
    '<a href="https://other.com/about">Elsewhere</a><a href="/report.pdf">About report</a>'
    "<p>" + "Filler text about the group. " * 20 + "</p></body></html>"
)
LEADERS = (
    "<html><head><title>Leadership | Example</title></head><body>"
    "<p>Asha Rao is the Managing Director and CEO of Example Limited and has led it since 2019.</p>"
    "<p>Example Limited was founded in 1954 in Pune and listed on the exchanges in 1994.</p>"
    + "<p>More filler text for length. </p>" * 12
    + "</body></html>"
)


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch(self, url, *, source, validate=None, **kw):
        self.fetched.append(url)
        body = self.pages.get(url.rstrip("/"))
        if body is None:
            return FetchResult(url, FetchStatus.NOT_FOUND, 404)
        return FetchResult(url, FetchStatus.OK, 200, body.encode(), "text/html", final_url=url)


class Router:
    providers = [object()]

    def __init__(self, data):
        self.data = data
        self.calls = []

    def complete_json(self, request):
        from app.llm.base import LlmResponse

        self.calls.append(request.user[:80])
        return LlmResponse(data=self.data, raw_text="", provider="fake", model="m1")


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    c = session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))
    c.website = "https://www.example.com"
    session.commit()
    return c


QUOTE = "Asha Rao is the Managing Director and CEO of Example Limited and has led it since 2019."
GOOD = {
    "leadership": [{"name": "Asha Rao", "role": "Managing Director and CEO", "quote": QUOTE}],
    "history": [
        {
            "quote": "Example Limited was founded in 1954 in Pune and listed on the exchanges in 1994."
        }
    ],
    "business": [],
}


def test_only_same_domain_about_pages_are_candidates():
    urls = enrich.candidate_pages(HOME, "https://www.example.com")
    assert urls == ["https://www.example.com/about-us", "https://www.example.com/leadership"]


def test_verified_quotes_are_stored_with_their_source(session, company):
    pages = {
        "https://www.example.com": HOME,
        "https://www.example.com/leadership": LEADERS,
        "https://www.example.com/about-us": LEADERS,
    }
    rep = enrich.enrich_company(session, company, Client(pages), Router(GOOD))
    assert rep.pages_read == 3 and rep.stored > 0 and rep.discarded >= 0
    out = enrich.load(session, company.id)
    lead = out["leadership"][0]
    assert (lead["name"], lead["role"]) == ("Asha Rao", "Managing Director and CEO")
    assert lead["publisher"] == "example.com" and lead["url"].startswith("https://www.example.com/")
    assert lead["retrieved"] and lead["extractor"] == "fake/m1"
    assert "founded in 1954" in out["history"][0]["quote"]


def test_a_quote_not_in_the_page_is_discarded_never_stored(session, company):
    invented = {
        "leadership": [
            {
                "name": "Vikram Shah",
                "role": "Chairman",
                "quote": "Vikram Shah is the Chairman of Example Limited.",
            },
            {"name": "Asha Rao", "role": "Chief Financial Officer", "quote": QUOTE},
        ],
        "history": [
            {"quote": "Example Limited was founded in 1901 in Mumbai."},
            {"quote": ""},
            "not a dict",
        ],
        "business": [{"quote": "x" * 500}],
    }
    rep = enrich.enrich_company(
        session, company, Client({"https://www.example.com": LEADERS}), Router(invented)
    )
    assert rep.stored == 0 and rep.discarded >= 6  # invented name, wrong role, wrong year, ...
    assert not session.scalars(select(CompanyEnrichment)).all()
    assert enrich.load(session, company.id)["available"] is False


def test_nothing_happens_without_a_website_or_a_model(session, company):
    company.website = None
    session.commit()
    assert (
        "no website" in enrich.enrich_company(session, company, Client({}), Router(GOOD)).notes[0]
    )
    company.website = "https://www.example.com"
    session.commit()
    assert "no language model" in enrich.enrich_company(session, company, Client({}), None).notes[0]


def test_a_failing_model_is_reported_and_nothing_is_stored(session, company):
    from app.llm.base import LlmError

    class Down(Router):
        def complete_json(self, request):
            raise LlmError("all providers failed")

    client = Client({"https://www.example.com": LEADERS})
    rep = enrich.enrich_company(session, company, client, Down({}))
    assert rep.stored == 0 and any("unavailable" in n for n in rep.notes)


def test_reading_a_page_again_replaces_its_rows_instead_of_duplicating(session, company):
    client = Client({"https://www.example.com": LEADERS})
    enrich.enrich_company(session, company, client, Router(GOOD))
    first = len(session.scalars(select(CompanyEnrichment)).all())
    enrich.enrich_company(session, company, client, Router(GOOD))
    assert len(session.scalars(select(CompanyEnrichment)).all()) == first


def test_the_stored_quotes_reach_the_report_with_their_source(session, company):
    from app.research import profile
    from app.research.pdf_pro import render
    from tests.test_pdf_pro import KW
    from tests.test_report_context import _axis, pdf_text

    client = Client({"https://www.example.com": LEADERS})
    enrich.enrich_company(session, company, client, Router(GOOD))
    content = _axis()
    content["analysis"]["profile"]["external"] = profile._external(session, company.id)
    text = pdf_text(render(content, mode="COMPLETE", **KW))[0].replace("\n", " ")
    assert "From the company's website" in text and "Asha Rao" in text
    assert "example.com, retrieved" in text and "not independent research" in text
