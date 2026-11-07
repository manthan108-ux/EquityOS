"""Controlled five-company test universe.

These are test companies, not a restriction: the architecture supports any NSE/BSE listed company.

Provenance (stored in `master_data_source`): BSE scrip codes were cross-checked against public
secondary sources (index constituent lists, broker pages); every ISIN passes the ISO 6166 check
digit. They have NOT yet been verified against primary NSE/BSE master files, which is Phase 4 work.
Fields we could not verify (investor-relations URLs, Reliance's founding year) are left unset.
"""

SOURCE = (
    "Seed: BSE codes cross-checked vs public secondary sources; ISIN check digits validated; "
    "pending primary NSE/BSE verification (Phase 4)"
)

# aliases: (text, alias_type)
TEST_UNIVERSE = [
    {
        "legal_name": "Reliance Industries Limited",
        "common_name": "Reliance Industries",
        "sector": "oil_and_gas",
        "industry": "refineries_and_marketing",
        "website": "https://www.ril.com",
        "headquarters": "Mumbai",
        "founding_year": None,  # 1957 vs 1973 incorporation history is ambiguous; left unverified
        "business_classification": (
            "Diversified conglomerate: oil-to-chemicals, digital services and retail"
        ),
        "nse_symbol": "RELIANCE",
        "bse_symbol": "RELIANCE",
        "bse_code": "500325",
        "isin": "INE002A01018",
        "aliases": [
            ("Reliance Industries Limited", "LEGAL_NAME"),
            ("Reliance Industries", "COMMON_NAME"),
            ("Reliance", "COMMON_NAME"),
            ("RIL", "ABBREVIATION"),
        ],
    },
    {
        "legal_name": "Tata Consultancy Services Limited",
        "common_name": "TCS",
        "sector": "it_services",
        "industry": "it_services_and_consulting",
        "website": "https://www.tcs.com",
        "headquarters": "Mumbai",
        "founding_year": 1968,
        "business_classification": "IT services, consulting and business solutions",
        "nse_symbol": "TCS",
        "bse_symbol": "TCS",
        "bse_code": "532540",
        "isin": "INE467B01029",
        "aliases": [
            ("Tata Consultancy Services Limited", "LEGAL_NAME"),
            ("Tata Consultancy Services", "COMMON_NAME"),
            ("Tata Consultancy", "COMMON_NAME"),
            ("TCS", "ABBREVIATION"),
        ],
    },
    {
        "legal_name": "Infosys Limited",
        "common_name": "Infosys",
        "sector": "it_services",
        "industry": "it_services_and_consulting",
        "website": "https://www.infosys.com",
        "headquarters": "Bengaluru",
        "founding_year": 1981,
        "business_classification": "IT services, consulting and digital solutions",
        "nse_symbol": "INFY",
        "bse_symbol": "INFY",
        "bse_code": "500209",
        "isin": "INE009A01021",
        "aliases": [
            ("Infosys Limited", "LEGAL_NAME"),
            ("Infosys", "COMMON_NAME"),
            ("Infy", "ABBREVIATION"),
            ("Infosys Technologies Limited", "FORMER_NAME"),
        ],
    },
    {
        "legal_name": "HDFC Bank Limited",
        "common_name": "HDFC Bank",
        "sector": "banking",
        "industry": "private_sector_bank",
        "website": "https://www.hdfcbank.com",
        "headquarters": "Mumbai",
        "founding_year": 1994,
        "business_classification": "Private sector bank: retail, wholesale and treasury banking",
        "nse_symbol": "HDFCBANK",
        "bse_symbol": "HDFCBANK",
        "bse_code": "500180",
        "isin": "INE040A01034",
        "aliases": [
            ("HDFC Bank Limited", "LEGAL_NAME"),
            ("HDFC Bank", "COMMON_NAME"),
        ],
    },
    {
        "legal_name": "ICICI Bank Limited",
        "common_name": "ICICI Bank",
        "sector": "banking",
        "industry": "private_sector_bank",
        "website": "https://www.icicibank.com",
        "headquarters": "Mumbai",
        "founding_year": 1994,
        "business_classification": "Private sector bank: retail, corporate and treasury banking",
        "nse_symbol": "ICICIBANK",
        "bse_symbol": "ICICIBANK",
        "bse_code": "532174",
        "isin": "INE090A01021",
        "aliases": [
            ("ICICI Bank Limited", "LEGAL_NAME"),
            ("ICICI Bank", "COMMON_NAME"),
        ],
    },
]
