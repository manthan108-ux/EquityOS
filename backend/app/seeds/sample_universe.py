"""The 15-company research sample used to build and test the peer and sector engines (Phase 8).

Why 15 and not 5: peers and sector weights are only meaningful with several companies in a sector.
The sample has five banks and five IT-services companies (real peer groups), plus a pair each
of FMCG and automobiles and Reliance as the one energy conglomerate. It is a sample, not a limit:
the platform supports every NSE/BSE listed company.

Provenance of the fields below (be clear about what they are):

* `sector` / `industry`: CURATED by the project from public knowledge of what each company does.
  They are not an exchange classification. The Phase 8 sector engine owns the real assignment;
  these only give the sample a starting point and are written ONLY into empty fields.
* `website`: the company's public corporate site; reachability was probed on 2026-11-08 with the
  polite client (robots.txt honoured). It says nothing about a company's other sites.
* `documents`: how filings can be obtained under the project's access rules. AUTOMATIC means the
  investor pages expose document links to our polite client; MANUAL means the site refused
  (HTTP 403), is closed to robots by robots.txt, or exposes nothing, and filings must be
  downloaded in a browser into `data/inbox/<SYMBOL>/` and ingested. STORED means filings are
  already in the database.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SampleCompany:
    symbol: str  # NSE symbol
    cohort: str  # the peer group the sample uses it for
    sector: str  # reference sector code
    industry: str  # reference industry code
    website: str
    documents: str  # AUTOMATIC | MANUAL | STORED
    note: str = ""


SAMPLE: tuple[SampleCompany, ...] = (
    # ---- banks -------------------------------------------------------------------------------
    SampleCompany(
        "ICICIBANK",
        "banks",
        "banking",
        "private_sector_bank",
        "https://www.icicibank.com",
        "STORED",
    ),
    SampleCompany(
        "HDFCBANK",
        "banks",
        "banking",
        "private_sector_bank",
        "https://www.hdfcbank.com",
        "MANUAL",
        "investor pages list documents but every PDF download timed out",
    ),
    SampleCompany(
        "KOTAKBANK",
        "banks",
        "banking",
        "private_sector_bank",
        "https://www.kotak.com",
        "AUTOMATIC",
        "198 document links found",
    ),
    SampleCompany(
        "AXISBANK",
        "banks",
        "banking",
        "private_sector_bank",
        "https://www.axisbank.com",
        "MANUAL",
        "investor pages expose only two unrelated documents",
    ),
    SampleCompany(
        "SBIN",
        "banks",
        "banking",
        "public_sector_bank",
        "https://sbi.co.in",
        "MANUAL",
        "investor pages not found (404)",
    ),
    # ---- IT services -------------------------------------------------------------------------
    SampleCompany(
        "TCS",
        "it_services",
        "it_services",
        "it_services_and_consulting",
        "https://www.tcs.com",
        "MANUAL",
        "HTTP 403 to automated access",
    ),
    SampleCompany(
        "INFY",
        "it_services",
        "it_services",
        "it_services_and_consulting",
        "https://www.infosys.com",
        "MANUAL",
        "HTTP 403 to automated access",
    ),
    SampleCompany(
        "WIPRO",
        "it_services",
        "it_services",
        "it_services_and_consulting",
        "https://www.wipro.com",
        "AUTOMATIC",
        "quarterly results and transcripts found",
    ),
    SampleCompany(
        "TECHM",
        "it_services",
        "it_services",
        "it_services_and_consulting",
        "https://www.techmahindra.com",
        "AUTOMATIC",
        "FY2026 integrated annual report found",
    ),
    SampleCompany(
        "HCLTECH",
        "it_services",
        "it_services",
        "it_services_and_consulting",
        "https://www.hcltech.com",
        "MANUAL",
        "closed to robots by robots.txt",
    ),
    # ---- other sectors (pairs where possible) ---------------------------------------------------
    SampleCompany(
        "RELIANCE",
        "energy",
        "oil_and_gas",
        "refineries_and_marketing",
        "https://www.ril.com",
        "STORED",
        "conglomerate; no close peer in the sample",
    ),
    SampleCompany(
        "ITC",
        "fmcg",
        "fmcg",
        "diversified_fmcg",
        "https://www.itcportal.com",
        "AUTOMATIC",
        "results and presentations found",
    ),
    SampleCompany(
        "HINDUNILVR",
        "fmcg",
        "fmcg",
        "household_and_personal_products",
        "https://www.hul.co.in",
        "MANUAL",
        "HTTP 403 to automated access",
    ),
    SampleCompany(
        "M&M",
        "automobiles",
        "automobiles",
        "automobile_manufacturers",
        "https://www.mahindra.com",
        "AUTOMATIC",
        "quarterly results found",
    ),
    SampleCompany(
        "MARUTI",
        "automobiles",
        "automobiles",
        "automobile_manufacturers",
        "https://www.marutisuzuki.com",
        "MANUAL",
        "investor pages not found (404)",
    ),
)

# Industries the sample needs that the reference data did not have yet (added, never changed).
NEW_INDUSTRIES: dict[tuple[str, str], str] = {
    ("banking", "public_sector_bank"): "Public Sector Bank",
    ("fmcg", "diversified_fmcg"): "Diversified FMCG",
    ("fmcg", "household_and_personal_products"): "Household and Personal Products",
    ("automobiles", "automobile_manufacturers"): "Automobile Manufacturers",
}

BY_SYMBOL = {c.symbol: c for c in SAMPLE}
