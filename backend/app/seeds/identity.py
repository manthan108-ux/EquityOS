"""Company identity records for the research sample: who is listed, since when, and what happened.

Sources: the NSE equity master (`data/master/nse_EQUITY_L.csv`: symbol, legal name, ISIN, listing
date) for the registry fields, and public reporting of the corporate actions for the notes. The
registry is a CHECK on the database, not a replacement for it: `verify_identity` compares each entry
with the stored security (symbol, ISIN, legal name) and reports differences instead of fixing them.

`comparable_from` is the first date from which the entity's own statements describe the same
business as today. Financial values for periods before it are kept but must not feed growth or
trend metrics unless the company itself restated them. A predecessor's statements are never
attributed to a different legal entity: only entities with an unbroken ISIN/legal-entity link
appear here.
"""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Identity:
    symbol: str
    isin: str
    legal_name: str  # as in the NSE master on 2026-11-10
    listed_on: date  # as in the NSE master
    relationship: str  # how history relates to the listed entity
    comparable_from: date | None = None  # None: history is continuous
    note: str = ""


IDENTITIES: dict[str, Identity] = {
    "NESTLEIND": Identity(
        "NESTLEIND",
        "INE239A01024",
        "Nestle India Limited",
        date(2023, 8, 1),
        "SAME_ENTITY_NEW_ISIN",
        date(2024, 4, 1),
        "The NSE master shows a 2023 listing date for the current ISIN (face value Re 1), which "
        "follows a share split; the legal entity and its statements are continuous. Nestle India "
        "moved its financial year from December to March: the first March year-end covers a longer "
        "transition period, so growth measured against it (or against the December 2022 year) "
        "compares unequal lengths and is excluded; growth is used only from the next full year.",
    ),
    "BRITANNIA": Identity(
        "BRITANNIA",
        "INE216A01030",
        "Britannia Industries Limited",
        date(1998, 11, 5),
        "CONTINUOUS",
    ),
    "GODREJCP": Identity(
        "GODREJCP",
        "INE102D01028",
        "Godrej Consumer Products Limited",
        date(2001, 6, 20),
        "CONTINUOUS",
    ),
    "TMPV": Identity(
        "TMPV",
        "INE155A01022",
        "Tata Motors Passenger Vehicles Limited",
        date(1998, 7, 22),
        "RENAMED_AFTER_DEMERGER",
        date(2025, 7, 1),
        "This is the original Tata Motors Limited (same ISIN), renamed after its "
        "commercial-vehicle business was demerged into a new company listed as TMCV "
        "(ISIN INE1TAE01010, listed 12-Nov-2025). Appointed date 1-Jul-2025, effective "
        "1-Oct-2025. Statements for periods "
        "before 1-Jul-2025 include the commercial-vehicle business; they are kept as stored but "
        "excluded from growth and trend metrics unless the company restated them. TMCV is NOT part "
        "of the sample and no TMCV figure is attributed to TMPV.",
    ),
    "HYUNDAI": Identity(
        "HYUNDAI",
        "INE0V6F01027",
        "Hyundai Motor India Limited",
        date(2024, 10, 22),
        "RECENT_LISTING",
        None,
        "Listed 22-Oct-2024: annual statements exist for earlier years (offer documents) but the "
        "listed-company filing history is short, so multi-year growth metrics are often gaps.",
    ),
    "TVSMOTOR": Identity(
        "TVSMOTOR",
        "INE494B01023",
        "TVS Motor Company Limited",
        date(2000, 8, 2),
        "CONTINUOUS",
    ),
    "ONGC": Identity(
        "ONGC",
        "INE213A01029",
        "Oil & Natural Gas Corporation Limited",
        date(1995, 7, 19),
        "CONTINUOUS",
    ),
    "IOC": Identity(
        "IOC",
        "INE242A01010",
        "Indian Oil Corporation Limited",
        date(1996, 7, 24),
        "CONTINUOUS",
    ),
    "BPCL": Identity(
        "BPCL",
        "INE029A01011",
        "Bharat Petroleum Corporation Limited",
        date(1995, 9, 13),
        "CONTINUOUS",
    ),
}

# Symbols that look related to a sample company but must never be merged into it.
NOT_THE_SAME_ENTITY: dict[str, str] = {
    "TMCV": "Tata Motors Limited (commercial vehicles), a separate legal entity since the demerger",
}


def comparable_from(symbol: str) -> date | None:
    ident = IDENTITIES.get(symbol)
    return ident.comparable_from if ident else None


def verify_identity(session) -> list[str]:
    """Differences between the registry and the stored securities (empty list = all match)."""
    from sqlalchemy import select

    from app.models import Company, Exchange, Security

    nse = session.scalar(select(Exchange.id).where(Exchange.code == "NSE"))
    problems: list[str] = []
    for symbol, ident in IDENTITIES.items():
        sec = session.scalar(
            select(Security).where(Security.exchange_id == nse, Security.symbol == symbol)
        )
        if sec is None:
            problems.append(f"{symbol}: not in the security master")
            continue
        if getattr(sec, "isin", None) and sec.isin != ident.isin:
            problems.append(f"{symbol}: ISIN {sec.isin} != expected {ident.isin}")
        company = session.get(Company, sec.company_id)
        stored = (company.legal_name or "").strip().lower() if company else ""
        if stored and stored != ident.legal_name.lower():
            problems.append(f"{symbol}: legal name {company.legal_name!r} != {ident.legal_name!r}")
    return problems
