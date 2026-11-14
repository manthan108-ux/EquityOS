"""Reprocess a stored document without ever making the stored evidence worse.

A normal re-extraction REPLACES the document's earlier promoted facts (rules may have changed). That
is right for a first run but unsafe for a batch re-read after a parser change: a new parser or a
non-deterministic LLM header reading can drop or alter figures that were already validated.

`reprocess_document` therefore runs the extraction inside a savepoint and keeps the new result only
when, for this document, it

* loses NO fact that was promoted before, and
* changes NO promoted value.

Otherwise the savepoint is rolled back and the previous extraction and facts stand exactly as they
were. Nothing is ever deleted or overwritten when the outcome is not a pure improvement.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.docintel.pipeline import PROVIDER_CODE, extract_document
from app.models import Document, ExtractedFact, FinancialFact

IMPROVED = "IMPROVED"  # new facts were added, nothing lost or changed: kept
UNCHANGED = "UNCHANGED"  # the same facts as before: kept (harmless)
KEPT_PREVIOUS = "KEPT_PREVIOUS"  # the new run would lose or change facts: rolled back
FAILED = "FAILED"  # the extraction itself failed: rolled back

Key = tuple[str, str, str, str, str]

# A fact the new read no longer promotes is acceptable only when it is still read and is withheld
# for a validation reason that makes the evidence safer: the document disagrees with itself, or a
# newer document supplies the period. Any other loss still rolls the whole re-read back.
WITHHELD_FOR = (
    "conflicts with another table in the same document",
    "a more recent document supplies this period",
    "entity not confirmed",  # a stricter attribution check: the table is someone else's
)


@dataclass
class ReprocessResult:
    status: str
    before: int = 0
    after: int = 0
    gained: list[Key] = field(default_factory=list)
    lost: list[Key] = field(default_factory=list)
    changed: list[Key] = field(default_factory=list)
    note: str = ""
    withheld: list[Key] = field(default_factory=list)  # lost, but withheld by a stricter check


def document_facts(session: Session, document_id) -> dict[Key, set[Decimal | None]]:
    """The promoted facts of one document, keyed by what they describe, with their value(s)."""
    rows = session.execute(
        select(
            FinancialFact.statement,
            FinancialFact.line_item,
            FinancialFact.period_end,
            FinancialFact.period_type,
            FinancialFact.basis,
            FinancialFact.value_inr,
        ).where(
            FinancialFact.provider_code == PROVIDER_CODE,
            FinancialFact.document_id == document_id,
        )
    )
    out: dict[Key, set[Decimal | None]] = {}
    for statement, item, end, ptype, basis, value in rows:
        key = (str(statement), item, str(end), str(ptype), str(basis))
        out.setdefault(key, set()).add(value)
    return out


def _withheld(session: Session, extraction_id: int, key: Key) -> bool:
    """True when the new read still extracts this fact but withholds it for a validation reason."""
    statement, item, end, _ptype, basis = key
    rows = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.extraction_id == extraction_id,
            ExtractedFact.statement == statement,
            ExtractedFact.canonical_item == item,
            ExtractedFact.basis == basis,
            ExtractedFact.period_end == date.fromisoformat(end),
        )
    ).all()
    return (
        bool(rows)
        and not any(r.promoted for r in rows)
        and all((r.not_promoted_reason or "").startswith(WITHHELD_FOR) for r in rows)
    )


def reprocess_document(
    session: Session, document: Document, data_dir: Path, *, llm_router=None
) -> ReprocessResult:
    before = document_facts(session, document.id)
    savepoint = session.begin_nested()
    try:
        run = extract_document(session, document, data_dir, llm_router=llm_router).extraction
        after = document_facts(session, document.id)
    except Exception as exc:  # noqa: BLE001 - nothing from a failed re-read may survive
        savepoint.rollback()
        return ReprocessResult(FAILED, len(before), len(before), note=type(exc).__name__)
    gained = [k for k in after if k not in before]
    lost = [k for k in before if k not in after]
    changed = [k for k in before if k in after and before[k] != after[k]]
    run_id = getattr(run, "id", None)  # no stored extraction, nothing can explain a loss
    withheld = [k for k in lost if run_id is not None and _withheld(session, run_id, k)]
    lost = [k for k in lost if k not in withheld]
    result = ReprocessResult(IMPROVED, len(before), len(after), gained, lost, changed)
    result.withheld = withheld
    if run.status.value == "FAILED":
        savepoint.rollback()
        result.status, result.after = FAILED, len(before)
        result.note = run.error or "extraction failed"
    elif lost or changed:
        savepoint.rollback()
        result.status, result.after = KEPT_PREVIOUS, len(before)
        result.note = (
            f"the new read would lose {len(lost)} and change {len(changed)} stored facts; "
            "the previous extraction was kept"
        )
    else:
        savepoint.commit()
        if not gained and not withheld:
            result.status = UNCHANGED
        if withheld:
            result.note = (
                f"{len(withheld)} fact(s) are now withheld: the document conflicts with itself or "
                "a newer document supplies the period"
            )
    return result
