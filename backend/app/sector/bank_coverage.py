"""Which bank-specific figures exist for each bank, and where the missing ones would come from.

A bank is scored on bank metrics only: a missing bank figure stays a gap and is never replaced by a
non-bank metric (debt/equity, EBITDA margin, current ratio). This module reports, per bank and per
concept, whether validated evidence exists, so the gap is visible and its remedy is named.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, EvidenceRecord, EvidenceStatus, Security


@dataclass(frozen=True)
class BankConcept:
    code: str
    label: str
    evidence_metrics: tuple[str, ...]  # any one of these makes the concept available
    source: str  # where it comes from when missing


CONCEPTS: tuple[BankConcept, ...] = (
    BankConcept(
        "interest_earned",
        "Interest earned",
        ("INCOME.interest_earned",),
        "bank-format financial results (P&L)",
    ),
    BankConcept(
        "interest_expended",
        "Interest expended",
        ("INCOME.interest_expended",),
        "bank-format financial results (P&L)",
    ),
    BankConcept(
        "nii",
        "Net interest income",
        ("INCOME.net_interest_income",),
        "derived from interest earned and expended",
    ),
    BankConcept(
        "provisions",
        "Provisions and contingencies",
        ("INCOME.provisions_excluding_tax", "INCOME.provisions_and_contingencies"),
        "bank-format financial results (P&L)",
    ),
    BankConcept("deposits", "Deposits", ("BALANCE_SHEET.deposits",), "bank-format balance sheet"),
    BankConcept("advances", "Advances", ("BALANCE_SHEET.advances",), "bank-format balance sheet"),
    BankConcept(
        "nim",
        "Net interest margin",
        ("RATIOS.net_interest_margin",),
        "needs average earning assets; or the bank's reported NIM from its results presentation",
    ),
    BankConcept(
        "gross_npa",
        "Gross NPA ratio",
        ("RATIOS.gross_npa_ratio",),
        "regulatory disclosures / investor presentation (not in the statements themselves)",
    ),
    BankConcept(
        "net_npa",
        "Net NPA ratio",
        ("RATIOS.net_npa_ratio",),
        "regulatory disclosures / investor presentation",
    ),
    BankConcept(
        "capital_adequacy",
        "Capital adequacy ratio",
        ("RATIOS.capital_adequacy_ratio",),
        "Pillar 3 / regulatory disclosure in the results",
    ),
    BankConcept(
        "casa",
        "CASA ratio",
        ("RATIOS.casa_ratio",),
        "investor presentation or the deposit breakdown in the annual report",
    ),
)


def bank_symbols(session: Session) -> list[tuple[str, Company]]:
    out = []
    for company in session.scalars(select(Company).where(Company.sector.has(code="banking"))):
        symbol = session.scalar(select(Security.symbol).where(Security.company_id == company.id))
        out.append((symbol or company.common_name, company))
    return sorted(out)


def coverage(session: Session) -> dict[str, dict[str, bool]]:
    """{symbol: {concept code: validated evidence exists}} for every bank with stored data."""
    result: dict[str, dict[str, bool]] = {}
    for symbol, company in bank_symbols(session):
        metrics = set(
            session.scalars(
                select(EvidenceRecord.metric)
                .where(
                    EvidenceRecord.company_id == company.id,
                    EvidenceRecord.status == EvidenceStatus.ACTIVE,
                )
                .distinct()
            )
        )
        result[symbol] = {c.code: any(m in metrics for m in c.evidence_metrics) for c in CONCEPTS}
    return result
