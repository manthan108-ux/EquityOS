# ruff: noqa: E501
"""API for the valuation assumption ledger: list, enter, edit, withdraw and read the history of inputs.

Every call needs an API key (X-API-Key). The author recorded on an input is the authenticated client's
name, not free text, so an input is attributable to whoever's key made it. Entering a value for an input
that already has one EDITS it: the old record is kept as SUPERSEDED and the history is queryable. There is
no default for any input; an input that was never entered simply does not exist.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_session
from app.models import Company
from app.research import assumptions as asm
from app.research.security import Principal, current_principal, rate_limited

router = APIRouter(prefix="/api", tags=["valuation-inputs"])
SessionDep = Annotated[Session, Depends(get_session)]
Caller = Annotated[Principal, Depends(current_principal)]
WriteCaller = Annotated[Principal, Depends(rate_limited("write"))]


class InputIn(BaseModel):
    input: str = Field(
        description="risk_free_rate | equity_risk_premium | beta | cost_of_equity | terminal_growth | roe_shift"
    )
    value: Decimal = Field(description="percent points (7 for 7%); beta as a plain number")
    as_of: date = Field(
        description="the date the figure refers to (a yield's date, the date a judgement was made)"
    )
    rationale: str = Field(
        min_length=asm.MIN_RATIONALE, description="why this value applies to this company"
    )
    scenario: str | None = Field(
        default=None, description="bear | base | bull, for the per-scenario inputs"
    )
    source: str | None = Field(
        default=None,
        description="the named source; with it the input is SOURCED, without it a USER assumption",
    )
    source_url: str | None = None
    edit_reason: str | None = None


class WithdrawIn(BaseModel):
    reason: str = Field(min_length=3)


def _company(session: Session, company_id: uuid.UUID) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="no such company")
    return company


@router.get("/companies/{company_id}/valuation-inputs")
def list_inputs(company_id: uuid.UUID, session: SessionDep, who: Caller) -> dict:
    _company(session, company_id)
    inputs = asm.current_inputs(session, company_id)
    capm = {"completed": False, "missing": [asm.SPECS[n].label for n in ("risk_free_rate", "equity_risk_premium", "beta") if f"{asm.PREFIX}.{n}" not in inputs]}  # fmt: skip
    return {
        "inputs": list(inputs.values()),
        "ledger": asm.ledger_view(inputs, capm) | {"capm": capm},
        "known_inputs": [
            {
                "input": s.name,
                "label": s.label,
                "unit": s.unit,
                "per_scenario": s.scenario,
                "market_input": s.market,
                "range": [str(s.low), str(s.high)],
                "max_age_days": s.max_age_days,
            }
            for s in asm.SPECS.values()
        ],  # fmt: skip
        "note": "nothing is assumed: an input that has not been entered does not exist, and a valuation that needs it is not completed",
    }


@router.put("/companies/{company_id}/valuation-inputs")
def enter_input(
    company_id: uuid.UUID, body: InputIn, session: SessionDep, who: WriteCaller
) -> dict:
    """Enter a valuation input, or edit it (the old value is kept as superseded)."""
    _company(session, company_id)
    try:
        record = asm.set_input(
            session, company_id, body.input, body.value, author=who.name, rationale=body.rationale,
            as_of=body.as_of, scenario=body.scenario, source=body.source, source_url=body.source_url,
            edit_reason=body.edit_reason,
        )  # fmt: skip
    except asm.AssumptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return asm.current_inputs(session, company_id)[record.metric]


@router.get("/companies/{company_id}/valuation-inputs/{name}/history")
def input_history(company_id: uuid.UUID, name: str, session: SessionDep, who: Caller, scenario: Annotated[str | None, Query()] = None) -> dict:  # fmt: skip
    _company(session, company_id)
    try:
        return {
            "input": name,
            "scenario": scenario,
            "history": asm.history(session, company_id, name, scenario),
        }
    except asm.AssumptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/valuation-inputs/{evidence_id}/withdraw")
def withdraw(evidence_id: int, body: WithdrawIn, session: SessionDep, who: WriteCaller) -> dict:
    """Retire an input without replacing it; valuations that need it become not completed."""
    try:
        record = asm.withdraw_input(session, evidence_id, body.reason, by=who.name)
    except asm.AssumptionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return {
        "evidence_id": record.id,
        "status": record.status.value,
        "status_reason": record.status_reason,
    }
