"""Static reference data: exchanges, research sectors and test-universe industries."""

EXCHANGES = [
    {"code": "NSE", "name": "National Stock Exchange of India", "mic": "XNSE"},
    {"code": "BSE", "name": "BSE Limited", "mic": "XBOM"},
]

# Research sectors named in the project specification. Each maps to a sector profile in Phase 8.
SECTORS = {
    "banking": "Banking",
    "nbfc": "NBFC",
    "insurance": "Insurance",
    "financial_services": "Financial Services",
    "it_services": "IT Services",
    "fmcg": "FMCG",
    "pharma": "Pharma",
    "healthcare": "Healthcare",
    "automobiles": "Automobiles",
    "oil_and_gas": "Oil and Gas",
    "metals": "Metals",
    "mining": "Mining",
    "power_utilities": "Power and Utilities",
    "telecom": "Telecom",
    "real_estate": "Real Estate",
    "capital_goods": "Capital Goods",
    "industrials": "Industrials",
    "chemicals": "Chemicals",
    "consumer_discretionary": "Consumer Discretionary",
}

# (sector_code, industry_code) -> name. Only industries needed so far; the rest arrive with
# the sector profiles.
INDUSTRIES = {
    ("oil_and_gas", "refineries_and_marketing"): "Refineries and Marketing",
    ("it_services", "it_services_and_consulting"): "IT Services and Consulting",
    ("banking", "private_sector_bank"): "Private Sector Bank",
}

# Source registry. authority_tier: 1 exchange files/filings, 2 company disclosures, 3 regulators,
# 4 government, 5 reputable secondary, 6 other public.
SOURCES = [
    {
        "code": "NSE_ARCHIVES",
        "name": "NSE static archive files (equity list, bhavcopy)",
        "authority_tier": 1,
        "base_url": "https://archives.nseindia.com/",
        "terms_note": "Public download path. Own research use only; do not redistribute.",
    },
    {
        "code": "BSE_BHAVCOPY",
        "name": "BSE static bhavcopy files",
        "authority_tier": 1,
        "base_url": "https://www.bseindia.com/download/BhavCopy/",
        "terms_note": "Public download path; BSE answers HTTP 200 error pages under bursts, so"
        " requests are spaced. Own research use only.",
    },
    {
        "code": "COMPANY_IR",
        "name": "Company websites and investor-relations pages",
        "authority_tier": 2,
        "base_url": None,
        "terms_note": "Each site's robots.txt and terms apply; access denials are not bypassed.",
    },
    {
        "code": "RBI_RSS",
        "name": "RBI press releases feed",
        "authority_tier": 3,
        "base_url": "https://www.rbi.org.in/",
        "terms_note": "Published RSS feed.",
    },
    {
        "code": "SEBI_RSS",
        "name": "SEBI announcements feed",
        "authority_tier": 3,
        "base_url": "https://www.sebi.gov.in/",
        "terms_note": "Published RSS feed.",
    },
    {
        "code": "MINT_RSS",
        "name": "Mint (Livemint) RSS feeds",
        "authority_tier": 5,
        "base_url": None,
        "terms_note": "Published RSS feed; headlines and links only, no article text copied.",
    },
    {
        "code": "ET_RSS",
        "name": "Economic Times RSS feed",
        "authority_tier": 5,
        "base_url": None,
        "terms_note": "Published RSS feed; headlines and links only, no article text copied.",
    },
    {
        "code": "MONEYCONTROL_RSS",
        "name": "Moneycontrol RSS feed",
        "authority_tier": 5,
        "base_url": None,
        "terms_note": "Published RSS feed; headlines and links only, no article text copied.",
    },
    {
        "code": "BUSINESSLINE_RSS",
        "name": "The Hindu BusinessLine RSS feeds",
        "authority_tier": 5,
        "base_url": None,
        "terms_note": "Published RSS feed; headlines and links only, no article text copied.",
    },
]
