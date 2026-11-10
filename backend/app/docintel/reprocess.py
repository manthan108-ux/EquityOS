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
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.docintel.pipeline import PROVIDER_CODE, extract_document
from app.models import Document, FinancialFact

IMPROVED = "IMPROVED"  # new facts were added, nothing lost or changed: kept
UNCHANGED = "UNCHANGED"  # the same facts as before: kept (harmless)
KEPT_PREVIOUS = "KEPT_PREVIOUS"  # the new run would lose or change facts: rolled back
FAILED = "FAILED"  # the extraction itself failed: rolled back

Key = tuple[str, str, str, str, str]


@dataclass
class ReprocessResult:
    status: str
    before: int = 0
    after: int = 0
    gained: list[Key] = field(default_factory=list)
    lost: list[Key] = field(default_factory=list)
    changed: list[Key] = field(default_factory=list)
    note: str = ""


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
    result = ReprocessResult(IMPROVED, len(before), len(after), gained, lost, changed)
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
        if not gained:
            result.status = UNCHANGED
    return result
