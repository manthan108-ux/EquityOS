"""Website discovery: candidates, strict verification, idempotent registration, ambiguity flagged."""

# ruff: noqa: F811
import pytest
from sqlalchemy import select

from app.acquisition import website_discovery as wd
from app.acquisition.http import FetchResult, FetchStatus
from app.docintel.entity import company_names  # noqa: F401
from app.models import Company, CompanyWebsiteRegistry
from app.seeds.loader import seed_reference_data, seed_test_universe


def page(title: str, body: str = "", footer: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body><p>{body}</p><footer>{footer}</footer></body></html>"


class FakeClient:
    """Serves canned pages; anything else is 'not found'. Records what was fetched."""

    def __init__(
        self, pages: dict[str, str | FetchStatus], redirects: dict[str, str] | None = None
    ):
        self.pages, self.redirects, self.fetched = pages, redirects or {}, []

    def fetch(self, url, *, source, validate=None, **kw):
        self.fetched.append(url)
        value = self.pages.get(url.rstrip("/"))
        if value is None:
            return FetchResult(url, FetchStatus.NOT_FOUND, 404)
        if isinstance(value, FetchStatus):
            return FetchResult(url, value, None, error=value.value)
        return FetchResult(
            url,
            FetchStatus.OK,
            200,
            value.encode(),
            "text/html",
            final_url=self.redirects.get(url, url),
        )


class FakeRouter:
    def __init__(self, sites):
        self.sites, self.calls = sites, 0

    def complete_json(self, request):
        from app.llm.base import LlmResponse

        self.calls += 1
        return LlmResponse(
            data={"websites": self.sites}, raw_text="", provider="fake", model="fake"
        )


@pytest.fixture
def company(session):
    seed_reference_data(session)
    seed_test_universe(session)
    for c in session.scalars(select(Company)):
        c.website = c.investor_relations_url = None  # the seed universe ships with websites
    session.commit()
    return session.scalar(select(Company).where(Company.common_name == "Reliance Industries"))


# ---- pure checks -----------------------------------------------------------------------------------

NAMES = {"reliance industries limited", "reliance industries", "reliance"}


def test_a_one_word_name_alone_proves_nothing():
    # the Titan mistake: a wealth-management site called "Titan" is not Titan Company Limited
    names = {"titan company limited", "titan"}
    html = page("Titan | Wealth Management for Tech Professionals", "Titan helps engineers invest")
    assert wd.judge_page(html, names, None, "titan.com")[0] == "REJECTED"
    assert wd.distinctive_names(names) == {"titan company limited"}
    assert wd.distinctive_names({"asian paints limited", "asian paints", "asian"}) == {
        "asian paints limited",
        "asian paints",
    }


def test_title_or_copyright_plus_body_verifies():
    html = page(
        "Reliance Industries Limited | Home", "Reliance Industries Limited is a conglomerate"
    )
    outcome, signals = wd.judge_page(html, NAMES, None, "ril.com")
    assert outcome == wd.VERIFIED and signals["name_in_title"]
    html = page(
        "Welcome", "About Reliance Industries Limited", "© 2026 Reliance Industries Limited"
    )
    assert wd.judge_page(html, NAMES, None, "ril.com")[0] == wd.VERIFIED


def test_the_isin_printed_on_the_page_verifies():
    html = page("Investors", "Reliance Industries Limited ISIN INE002A01018")
    assert wd.judge_page(html, NAMES, "INE002A01018", "ril.com")[0] == wd.VERIFIED


def test_a_domain_that_is_the_name_plus_the_full_legal_name_verifies():
    names = {"dabur india limited", "dabur"}
    html = page("World's Best Ayurvedic Brand | Dabur", "Dabur India Limited")
    assert wd.judge_page(html, names, None, "dabur.com")[0] == wd.VERIFIED
    assert wd.judge_page(html, names, None, "other.com")[0] == wd.AMBIGUOUS  # name in text only


def test_a_name_in_the_text_alone_is_ambiguous_and_an_absent_name_is_rejected():
    parent = page("Group home", "Our subsidiaries include Reliance Industries Limited and others")
    assert wd.judge_page(parent, NAMES, None, "group.com")[0] == wd.AMBIGUOUS
    assert wd.judge_page(page("Cafe", "Coffee"), NAMES, None, "cafe.com")[0] == "REJECTED"


def test_registrable_domains_and_guesses():
    assert wd.registrable("https://investors.example.co.in/x") == "example.co.in"
    assert wd.registrable("https://www.ril.com") == "ril.com"
    guesses = wd.name_guesses({"asian paints limited", "asian paints"})
    assert "https://www.asianpaints.com" in guesses and "https://www.asian.com" in guesses
    assert all(g.startswith("https://www.") for g in guesses)


# ---- the workflow -----------------------------------------------------------------------------------


def test_a_verified_site_is_registered_with_its_investor_page_and_provenance(session, company):
    home = page(
        "Reliance Industries Limited",
        'Reliance Industries Limited <a href="/investors">Investor Relations</a>',
    )
    client = FakeClient(
        {
            "https://www.relianceindustries.com": home,
            "https://www.relianceindustries.com/investors": page("Investors", "results"),
        }
    )
    res = wd.discover(session, company, client, use_llm=False)
    assert res.status == wd.VERIFIED and res.source == wd.SOURCE_NAME
    assert company.website == "https://www.relianceindustries.com"
    assert company.investor_relations_url == "https://www.relianceindustries.com/investors"
    row = session.get(CompanyWebsiteRegistry, company.id)
    assert (
        row.status == wd.VERIFIED
        and row.verified_at
        and row.verification["signals"]["name_in_title"]
    )


def test_registration_is_idempotent_and_a_verified_url_is_never_replaced(session, company):
    company.website = "https://www.ril.com"
    session.commit()
    client = FakeClient(
        {
            "https://www.relianceindustries.com": page(
                "Reliance Industries Limited", "Reliance Industries Limited"
            )
        }
    )
    first = wd.discover(session, company, client, use_llm=False)
    assert first.note.startswith("already registered") and company.website == "https://www.ril.com"
    assert (
        client.fetched == ["https://www.ril.com"] or client.fetched == []
    )  # no candidate was even tried
    row = session.get(CompanyWebsiteRegistry, company.id)
    assert row.status == wd.MANUAL and row.discovery_source == wd.SOURCE_EXISTING
    wd.discover(session, company, client, use_llm=False)
    assert company.website == "https://www.ril.com"


def test_an_ambiguous_site_is_flagged_not_registered(session, company):
    client = FakeClient(
        {
            "https://www.relianceindustries.com": page(
                "Group", "a holding of Reliance Industries Limited"
            )
        }
    )
    res = wd.discover(session, company, client, use_llm=False)
    assert res.status == wd.AMBIGUOUS and company.website is None
    assert session.get(CompanyWebsiteRegistry, company.id).status == wd.AMBIGUOUS


def test_aggregators_and_redirects_to_them_are_rejected(session, company):
    client = FakeClient(
        {
            "https://www.relianceindustries.com": page(
                "Reliance Industries Limited", "Reliance Industries Limited"
            )
        },
        redirects={"https://www.relianceindustries.com": "https://www.moneycontrol.com/reliance"},
    )
    res = wd.discover(session, company, client, use_llm=False)
    assert res.status in (wd.FAILED, wd.NO_CANDIDATE) and company.website is None
    assert any(
        "not a company site" in t["reason"] or "aggregator" in t["reason"] for t in res.tried
    )


def test_a_private_address_from_the_llm_is_never_fetched(session, company):
    client = FakeClient({})
    router = FakeRouter(["http://127.0.0.1/admin", "http://localhost/x", "https://192.168.0.1"])
    wd.discover(session, company, client, router)
    assert not any(h in u for u in client.fetched for h in ("127.0.0.1", "localhost", "192.168"))


def test_the_llm_only_proposes_and_the_page_decides(session, company):
    router = FakeRouter(["https://www.ril-group.in"])
    good = page("Reliance Industries Limited", "Reliance Industries Limited")
    res = wd.discover(session, company, FakeClient({"https://www.ril-group.in": good}), router)
    assert res.status == wd.VERIFIED and res.source == wd.SOURCE_LLM and router.calls == 1

    other = session.scalar(select(Company).where(Company.id != company.id))
    wrong = FakeClient({"https://www.liar.com": page("Unrelated Pvt", "nothing here")})
    res2 = wd.discover(session, other, wrong, FakeRouter(["https://www.liar.com"]))
    assert res2.status != wd.VERIFIED and other.website is None


def test_a_domain_registered_to_another_company_is_ambiguous(session, company):
    other = session.scalar(select(Company).where(Company.id != company.id))
    other.website = "https://www.ril.com"
    session.commit()
    wd.record_existing(session, other)
    good = page("Reliance Industries Limited", "Reliance Industries Limited")
    res = wd.discover(
        session,
        company,
        FakeClient({"https://www.ril.com": good}),
        FakeRouter(["https://www.ril.com"]),
    )
    assert res.status == wd.AMBIGUOUS and company.website is None


def test_failures_back_off_and_are_not_retried_before_they_are_due(session, company):
    client = FakeClient({})
    first = wd.discover(session, company, client, use_llm=False)
    assert first.status in (wd.FAILED, wd.NO_CANDIDATE)
    row = session.get(CompanyWebsiteRegistry, company.id)
    assert row.attempts == 1 and row.next_retry_at is not None
    n = len(client.fetched)
    again = wd.discover(session, company, client, use_llm=False)
    assert "retry not due" in again.note and len(client.fetched) == n  # nothing was fetched
    wd.discover(session, company, client, use_llm=False, force=True)
    assert session.get(CompanyWebsiteRegistry, company.id).attempts == 2


def test_robots_blocked_candidates_are_reported_as_such(session, company):
    client = FakeClient(
        {
            u: FetchStatus.BLOCKED_BY_ROBOTS
            for u in wd.name_guesses(company_names(session, company.id))
        }
    )
    res = wd.discover(session, company, client, use_llm=False)
    assert "robots.txt" in res.note


def test_the_summary_counts_the_registry(session, company):
    company.website = "https://www.ril.com"
    session.commit()
    wd.record_existing(session, company)
    s = wd.registry_summary(session)
    assert s["with_website"] >= 1 and s["by_status"].get(wd.MANUAL) == 1
