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
