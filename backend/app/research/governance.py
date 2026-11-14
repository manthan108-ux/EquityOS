# ruff: noqa: E501
"""Management, corporate governance and capital allocation.

`analysis["governance"]` is built from three kinds of support and labels each:

* EVIDENCE: validated figures (promoter shareholding by quarter, dividends paid, buybacks, acquisitions
  and capital expenditure from the cash flow statement, profit, free cash flow) with evidence ids; and
  dividend announcements from the exchange corporate-action feed with their dates and amounts.
* DOCUMENT_QUOTE: a sentence quoted verbatim from the company's latest stored annual report, with its page
  (management background and tenure, board composition, related-party transactions, governance practices,
  capital-allocation policy, dividend and buyback policy, acquisitions).
* PARSED: a number read from such a sentence by a stated pattern (board size, independent directors),
  always shown next to the sentence it came from.

The governance assessment is a CHECKLIST of stated tests, each PASS, FLAG or UNKNOWN with the source and
the rule, and a count of each. There is no composite governance score: a number would claim more than the
stored evidence supports. UNKNOWN means the stored documents do not show it, not that the answer is bad.

Nothing about a person's background, tenure or track record is added beyond what the report states.
"""

import re
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CorporateAction, DocumentPage
from app.research import analysis as an
from app.research import mgmt
from app.research import profile as pr
from app.research import readiness as rd
from app.research.sectors import AMOUNT, PCT, Metric

VERSION = "1.0"
MAX_PER_TOPIC = 3
INDEPENDENT_MIN = Decimal(1) / Decimal(
    3
)  # SEBI: at least one third independent (one half if the chair is executive)
PROMOTER_FALL_PP = Decimal("5")  # a fall of 5 points over the window is flagged
COMMITTEES = ("Audit Committee", "Nomination and Remuneration Committee", "Stakeholders Relationship Committee", "Risk Management Committee")  # fmt: skip
NUMBER_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}  # fmt: skip

PROMOTER = Metric("promoter", "Promoter holding", ("SHAREHOLDING.promoters.percent",), PCT)
CFO = Metric("cfo", "Operating cash flow", ("CASH_FLOW.net_cash_from_operating_activities",), AMOUNT, ptype="ANNUAL")  # fmt: skip
CAPEX = Metric("capex", "Capital expenditure", ("CASH_FLOW.capital_expenditure",), AMOUNT, ptype="ANNUAL")  # fmt: skip
DIVIDENDS = Metric("dividends", "Dividends paid", ("CASH_FLOW.dividends_paid",), AMOUNT, ptype="ANNUAL")  # fmt: skip
BUYBACK = Metric("buyback", "Buyback of shares", ("CASH_FLOW.buyback_of_shares",), AMOUNT, ptype="ANNUAL")  # fmt: skip
ACQUISITIONS = Metric("acquisitions", "Acquisitions of subsidiaries and businesses", ("CASH_FLOW.acquisition_of_subsidiaries_and_businesses",), AMOUNT, ptype="ANNUAL")  # fmt: skip
PAT = Metric("pat", "Net profit attributable to owners", ("INCOME.attributable_profit",), AMOUNT, ptype="ANNUAL")  # fmt: skip
DEPRECIATION = Metric("dep", "Depreciation and amortisation", ("INCOME.depreciation_and_amortisation", "CASH_FLOW.depreciation_and_amortisation"), AMOUNT, ptype="ANNUAL")  # fmt: skip
GOODWILL = Metric("goodwill", "Goodwill", ("BALANCE_SHEET.goodwill", "BALANCE_SHEET.goodwill_on_consolidation"), AMOUNT, ptype="ANNUAL")  # fmt: skip
EQUITY = Metric("equity", "Owners' equity", ("BALANCE.owners_equity",), AMOUNT, ptype="ANNUAL")
NAMES = frozenset({n for m in (PROMOTER, CFO, CAPEX, DIVIDENDS, BUYBACK, ACQUISITIONS, PAT, DEPRECIATION, GOODWILL, EQUITY) for n in m.names})  # fmt: skip

# topic -> (title, cue regex, per-topic exclusion)
TOPICS: dict[str, tuple[str, str, str]] = {
    "background": (
        "Management and promoter background",
        r"\b(?:has|have|brings|bring) (?:over |more than |around |about )?\d{1,2}\+? years(?:'|’)? (?:of )?(?:experience|in)\b|\bwith (?:over |more than )?\d{1,2}\+? years of experience\b|"
        r"\bpromoter(?:s| group)?\b[^.]{0,60}\b(?:family|founded|group|holding company|belongs?)\b|\b(?:founder|co-founder|founding)\b[^.]{0,40}\b(?:chairman|promoter|director|family)\b|"
        r"\b(?:Mr|Ms|Mrs|Dr|Shri|Smt|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+[^.]{0,80}\b(?:is|was) (?:the |a )?(?:Chairman|Managing Director|CEO|Chief Executive|Executive Director|Whole[- ]time Director)\b",
        r"remuneration|sitting fee|attended|\bDIN\b|stock option",
    ),
    "tenure": (
        "Tenure and appointments",
        r"\b(?:was |were )?(?:appointed|re-appointed|reappointed|elevated)\b[^.]{0,80}\b(?:as|to)\b[^.]{0,60}\b(?:Chairman|Managing Director|Chief Executive|CEO|Chief Financial|CFO|Whole[- ]time Director|Executive Director|Independent Director|Director)\b|"
        r"\b(?:joined|has been with) (?:the )?(?:Company|Bank|Group|Board)\b[^.]{0,50}\b(?:in|since|from)\b[^.]{0,20}\b(?:19|20)\d{2}\b|\bsince (?:19|20)\d{2}\b[^.]{0,40}\b(?:Chairman|Managing Director|CEO|Director)\b|"
        r"\bterm of (?:five|5|three|3) years\b",
        r"remuneration|sitting fee|attended|\bDIN\b|stock option|Secretarial|Registrar|Auditor|auditors",
    ),
    "board": (
        "Board composition",
        r"\bBoard (?:of Directors )?(?:comprises|comprised|consists|consisted|is composed|comprising)\b|\bcomposition of the Board\b|\b(?:Independent|Non-Executive|Executive|Woman|Women) Directors?\b[^.]{0,60}\b(?:out of|of which|including|constitut\w+|comprise\w*)\b|"
        r"\bat least one (?:woman|women) director\b|\bBoard\b[^.]{0,40}\b(?:independence|independent directors?)\b",
        r"remuneration|sitting fee|attended|\bDIN\b|stock option|Regulation 17\(1\)\(a\) requires",
    ),
    "related_party": (
        "Related-party transactions",
        r"\brelated[- ]part(?:y|ies)\b[^.]{0,80}\b(?:transactions?|contracts?|arrangements?)\b|\barm'?s? length\b|\bmaterial related[- ]party\b|\bRegulation 23\b",
        r"",
    ),
    "governance": (
        "Governance practices",
        r"\b(?:corporate governance|code of conduct|whistle[- ]?blower|vigil mechanism|insider trading|independent (?:evaluation|assessment) of the Board|Board evaluation|performance evaluation of the Board|succession plan\w*|related party policy)\b",
        r"remuneration|sitting fee|attended|\bDIN\b|stock option|\bhe has\b|\bshe has\b|credit ratings?|talent",
    ),
    "capital_allocation": (
        "Capital allocation policy",
        r"\bcapital allocation\b|\bcapital deployment\b|\bdividend (?:distribution )?policy\b|\bpayout\b[^.]{0,40}\b(?:ratio|policy|of (?:the )?(?:net )?profit)\b|\breturn(?:s)? (?:of capital )?to (?:shareholders|stakeholders)\b|\bretain(?:ed|ing)? earnings\b|\bbalance sheet strength\b[^.]{0,40}\b(?:invest|growth)\b",
        r"remuneration|stock option|Foundation|Government|developed economies|global|budgetary|infra sector|investment flows",
    ),
    "dividend_buyback": (
        "Dividends and buybacks",
        r"\b(?:interim|final|special|recommended|declared|paid) dividend\b|\bdividend of (?:Rs\.?|₹|INR|`)\s?\d|\bbuy-?back\b|\brepurchase of (?:equity )?shares\b",
        r"unclaimed|Investor Education|IEPF|stock option|ESOP|dividend income|received",
    ),
    "acquisitions": (
        "Acquisitions and investments",
        r"\b(?:acquired|acquisition of|completed the acquisition|agreed to acquire|invested in|investment in) [^.]{0,80}\b(?:subsidiary|company|business|stake|shares|limited|inc|llc|platform)\b|\b(?:capital expenditure|capex|capital investment)\b[^.]{0,60}\b(?:of|was|stood|aggregat\w+|planned|during)\b[^.]{0,20}\d",
        r"fair value|impairment|goodwill on|purchase consideration allocation|Committee|stock option|Foundation|CSR",
    ),
}
_RX = {k: re.compile(v[1], re.I | re.S) for k, v in TOPICS.items()}
_SKIP_RX = {k: re.compile(v[2], re.I) for k, v in TOPICS.items() if v[2]}
_NOT_FORWARD = re.compile(r"forward[- ]looking|projections|expectations based", re.I)


_NEGATED_ARMS = re.compile(
    r"\bnot (?:at|on|in)\b[^.]{0,20}\b(?:arm'?s?|ordinary)|\bdetails of\b|\bother than\b[^.]{0,40}\barm",
    re.I,
)
_ARMS = re.compile(
    r"\b(?:at|on an?|on) arm'?s?[- ]length\b|\bin the ordinary course of business\b", re.I
)


def _arms_length(sentence: str) -> bool:
    """A positive statement that related-party transactions were at arm's length / in the ordinary course."""
    return bool(_ARMS.search(sentence)) and not _NEGATED_ARMS.search(sentence)


def _usable(sentence: str, topic: str) -> bool:
    if _NOT_FORWARD.search(sentence):
        return False
    if topic in _SKIP_RX and _SKIP_RX[topic].search(sentence):
        return False
    return True


def extract(pages: list[tuple[int, str]]) -> dict[str, list[dict]]:
    """Topic -> up to MAX_PER_TOPIC quoted sentences with their page; each sentence is used once."""
    pool: dict[str, list[tuple[int, int, str]]] = {k: [] for k in TOPICS}
    seen: set[str] = set()
    for page, text in pages:
        for s in mgmt.sentences(text):
            key = re.sub(r"\W+", " ", s.lower()).strip()
            if key in seen:
                continue
            seen.add(key)
            for topic, rx in _RX.items():
                if rx.search(s) and _usable(s, topic):
                    pool[topic].append((len(rx.findall(s)) * 2 + len(pr.YEAR.findall(s)), page, s))
    used: set[str] = set()
    out: dict[str, list[dict]] = {}
    for topic in TOPICS:
        chosen = []
        for _, page, s in sorted(pool[topic], key=lambda t: (-t[0], t[1])):
            if s in used:
                continue
            used.add(s)
            chosen.append({"source": "DOCUMENT_QUOTE", "text": s, "page": page})
            if len(chosen) == MAX_PER_TOPIC:
                break
        out[topic] = sorted(chosen, key=lambda c: c["page"])
    return out


# ---------------------------------------------------------------- board composition (parsed)


def _num(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


_NUM = r"(\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
_TOTAL = re.compile(rf"\b{_NUM}\s+(?:\(\d+\)\s+)?(?:Directors|members)\b", re.I)
_INDEP = re.compile(
    rf"\b{_NUM}\s+(?:\(\d+\)\s+)?(?:of whom |of which |are |were |being |as )?(?:are |were )?(?:Non-Executive,? )?Independent\b",
    re.I,
)
_WOMEN = re.compile(
    rf"\b{_NUM}\s+(?:\(\d+\)\s+)?(?:are |were )?(?:woman|women)\b|\b(?:woman|women) directors?\b",
    re.I,
)


def parse_board(pages: list[tuple[int, str]]) -> dict | None:
    """Board size and independent directors, only from ONE sentence that states both."""
    for page, text in pages:
        for s in mgmt.sentences(text):
            if not re.search(r"\bBoard\b", s):
                continue
            total, indep = _TOTAL.search(s), _INDEP.search(s)
            if not (total and indep):
                continue
            n, i = _num(total.group(1)), _num(indep.group(1))
            if n is None or i is None or n < 3 or n > 25 or i > n:
                continue
            women = _WOMEN.search(s)
            return {
                "source": "PARSED",
                "board_size": n,
                "independent": i,
                "independent_share": str((Decimal(i) / Decimal(n) * 100).quantize(Decimal("0.1"))),
                "woman_director_stated": bool(women),
                "text": s,
                "page": page,
                "rule": "board size and independent directors read from one sentence that states both numbers",
            }
    return None


def committees(pages: list[tuple[int, str]]) -> list[dict]:
    """Which of the listed-company committees the report mentions, with the first pages."""
    out = []
    for name in COMMITTEES:
        hits = [p for p, t in pages if re.search(re.escape(name), re.sub(r"\s+", " ", t), re.I)]
        out.append({"committee": name, "pages": hits[:4], "mentioned": bool(hits)})
    return out


# ---------------------------------------------------------------- promoter holding


def promoter_holding(store, basis: str | None = None) -> dict:
    recs = an._records(store, PROMOTER.names, "QUARTERLY", None) or an._records(
        store, PROMOTER.names, "ANNUAL", None
    )
    if not recs:
        for r in store.by_metric.get(PROMOTER.names[0], []):
            if r.value is not None and r.period_end is not None:
                recs[r.period_end] = r
    ends = sorted(recs)[-12:]
    if not ends:
        return {
            "available": False,
            "reason": "no promoter shareholding pattern is stored for this company",
        }
    series = [{"period_end": e.isoformat(), "value": an._s(recs[e].value.quantize(Decimal("0.01"))), "evidence_id": recs[e].id} for e in ends]  # fmt: skip
    out = {"available": True, "series": series, "latest": series[-1], "pledge": {"available": False, "reason": "promoter share pledges are not stored; they are disclosed in the shareholding pattern filing and not yet read"}}  # fmt: skip
    if recs[ends[-1]].value == 0:
        out["note"] = (
            "no promoter holding is reported: the company is widely held or professionally managed, so there is no promoter group to assess"
        )
    if len(ends) >= 2:
        first, last = recs[ends[0]], recs[ends[-1]]
        change = (last.value - first.value).quantize(Decimal("0.01"))
        out["change_pp"] = an._s(change)
        out["from"], out["to"] = ends[0].isoformat(), ends[-1].isoformat()
        out["direction"] = (
            "RISING"
            if change >= Decimal("0.5")
            else "FALLING"
            if change <= Decimal("-0.5")
            else "STABLE"
        )
        out["change_evidence_ids"] = [first.id, last.id]
    return out


# ---------------------------------------------------------------- capital allocation


def _annual(store, m: Metric, basis: str) -> dict:
    return {
        d: r for d, r in an._records(store, m.names, "ANNUAL", basis).items() if r.basis == basis
    }


def allocation(store, basis: str) -> dict:
    """How operating cash flow was used each year: capex, dividends, buybacks, acquisitions (as printed)."""
    cfo, capex = _annual(store, CFO, basis), _annual(store, CAPEX, basis)
    divs, buy, acq = (
        _annual(store, DIVIDENDS, basis),
        _annual(store, BUYBACK, basis),
        _annual(store, ACQUISITIONS, basis),
    )
    pat = _annual(store, PAT, basis)
    years = sorted(set(cfo) & (set(capex) | set(divs) | set(buy) | set(acq)))[-5:]
    if not years:
        return {"available": False, "reason": "operating cash flow together with at least one use of cash (capital expenditure, dividends, buybacks, acquisitions) is not stored for any year on this basis"}  # fmt: skip
    rows = []
    for end in years:
        c = cfo[end]
        row = {"fy": rd.fiscal_year(end), "cfo": an._s(c.value), "evidence_ids": [c.id], "uses": {}}
        for key, recs, _sign in (
            ("capex", capex, -1),
            ("dividends", divs, -1),
            ("buybacks", buy, -1),
            ("acquisitions", acq, -1),
        ):
            r = recs.get(end)
            if r is None:
                row["uses"][key] = None
                continue
            spend = abs(r.value)
            row["uses"][key] = {"amount": an._s(spend), "pct_of_cfo": an._s((spend / c.value * 100).quantize(Decimal("0.1"))) if c.value > 0 else None, "evidence_id": r.id}  # fmt: skip
            row["evidence_ids"].append(r.id)
        d, p = divs.get(end), pat.get(end)
        if d is not None and p is not None and p.value > 0:
            row["payout_pct"] = an._s((abs(d.value) / p.value * 100).quantize(Decimal("0.1")))
            row["payout_evidence_ids"] = [d.id, p.id]
        if c.value > 0 and end in capex:
            fcf = c.value + capex[end].value
            returned = sum(
                (abs(recs[end].value) for recs in (divs, buy) if end in recs), Decimal(0)
            )
            if returned:
                row["returned_vs_fcf"] = (
                    "COVERED" if returned <= max(fcf, Decimal(0)) else "EXCEEDS_FCF"
                )
        rows.append(row)
    missing = [label for label, recs in (("capital expenditure", capex), ("dividends", divs), ("buybacks", buy), ("acquisitions", acq)) if not recs]  # fmt: skip
    return {"available": True, "basis": basis, "years": rows, "not_stored": missing, "rules": "shares are of operating cash flow (only where it is positive); a use of cash is the absolute value of the stored cash flow line; payout = dividends paid / net profit attributable; 'exceeds FCF' = dividends + buybacks above operating cash flow less capital expenditure"}  # fmt: skip


def dividend_history(session: Session, company_id: uuid.UUID) -> dict:
    """Dividend announcements and buybacks the exchange feed holds, newest first."""
    rows = list(
        session.scalars(
            select(CorporateAction)
            .where(
                CorporateAction.company_id == company_id,
                CorporateAction.action_type.in_(("Dividend", "Buyback", "Buy Back")),
            )
            .order_by(CorporateAction.effective_date.desc())
        )
    )
    divs = []
    for r in rows:
        details = {d.get("name"): d.get("value") for d in (r.details or []) if isinstance(d, dict)}
        divs.append(
            {
                "type": r.action_type,
                "ex_date": r.effective_date.isoformat() if r.effective_date else None,
                "amount_per_share": str(r.amount) if r.amount is not None else None,
                "kind": details.get("Dividend type"),
                "details": details.get("Details"),
                "source": r.provider_code,
                "action_id": r.id,
            }
        )
    if not divs:
        return {
            "available": False,
            "reason": "no dividend or buyback announcement is stored from the exchange feed",
        }
    dates = [d["ex_date"] for d in divs if d["ex_date"]]
    return {
        "available": True,
        "events": divs[:12],
        "count": len(divs),
        "covers": f"{min(dates)} to {max(dates)}" if dates else None,
        "buybacks_stored": any(d["type"] != "Dividend" for d in divs),
        "note": "the stored feed covers a limited window (about the last year); it is not a complete dividend history, and no per-share total is derived from it",
    }


def capex_and_acquisitions(store, basis: str, analysis: dict) -> dict:
    capex, dep = _annual(store, CAPEX, basis), _annual(store, DEPRECIATION, basis)
    acq, gw, eq = (
        _annual(store, ACQUISITIONS, basis),
        _annual(store, GOODWILL, basis),
        _annual(store, EQUITY, basis),
    )
    out: dict = {"available": False, "observations": []}
    ends = sorted(capex)[-5:]
    if ends:
        rows = []
        for e in ends:
            row = {
                "fy": rd.fiscal_year(e),
                "capex": an._s(abs(capex[e].value)),
                "evidence_ids": [capex[e].id],
            }
            if e in dep and dep[e].value > 0:
                row["capex_to_depreciation"] = an._s(
                    (abs(capex[e].value) / dep[e].value).quantize(Decimal("0.01"))
                )
                row["evidence_ids"].append(dep[e].id)
            rows.append(row)
        out.update({"available": True, "capex": rows})
        last = rows[-1]
        if "capex_to_depreciation" in last:
            v = Decimal(last["capex_to_depreciation"])
            out["observations"].append({"text": f"{last['fy']} capital expenditure was {v}x depreciation: {'growing' if v > 1 else 'not growing'} the asset base.", "kind": "INTERPRETATION", "rule": "capex above depreciation grows the asset base", "evidence_ids": last["evidence_ids"]})  # fmt: skip
        roce = (((analysis.get("health") or {}).get("returns") or {}).get("metrics") or {}).get(
            "roce"
        ) or {}
        if roce.get("available") and len(roce["series"]) >= 2:
            a, b = roce["series"][0], roce["series"][-1]
            out["observations"].append({"text": f"Return on capital employed moved from {Decimal(a['value'])}% ({a['fy']}) to {Decimal(b['value'])}% ({b['fy']}) over the same period; the two are shown together, no causal link is claimed.", "kind": "INTERPRETATION", "rule": "capital expenditure and ROCE side by side", "evidence_ids": [a["evidence_id"], b["evidence_id"]]})  # fmt: skip
    a_ends = sorted(acq)[-5:]
    if a_ends:
        out["available"] = True
        out["acquisitions"] = [{"fy": rd.fiscal_year(e), "amount": an._s(abs(acq[e].value)), "evidence_id": acq[e].id} for e in a_ends]  # fmt: skip
    g_ends = sorted(gw)
    if len(g_ends) >= 2:
        first, last = gw[g_ends[0]], gw[g_ends[-1]]
        if first.value > 0:
            change = ((last.value / first.value - 1) * 100).quantize(Decimal("0.1"))
            share = None
            if g_ends[-1] in eq and eq[g_ends[-1]].value > 0:
                share = (last.value / eq[g_ends[-1]].value * 100).quantize(Decimal("0.1"))
            out["available"] = True
            out["goodwill"] = {"from_fy": rd.fiscal_year(g_ends[0]), "to_fy": rd.fiscal_year(g_ends[-1]), "change_pct": an._s(change), "share_of_equity_pct": an._s(share) if share is not None else None, "evidence_ids": [first.id, last.id]}  # fmt: skip
            out["observations"].append({"text": f"Goodwill changed by {change}% between {rd.fiscal_year(g_ends[0])} and {rd.fiscal_year(g_ends[-1])}" + (f" and is {share}% of owners' equity." if share is not None else "."), "kind": "INTERPRETATION", "rule": "goodwill growth is a marker of acquisitions; a large share of equity is value that depends on those acquisitions earning their cost", "evidence_ids": [first.id, last.id]})  # fmt: skip
    if not out["available"]:
        out["reason"] = (
            "no capital-expenditure, acquisition or goodwill figures are stored on this basis"
        )
    return out


# ---------------------------------------------------------------- the checklist


def checklist(
    *,
    promoter: dict,
    board: dict | None,
    comms: list[dict],
    quotes: dict,
    auditor_flags,
    alloc: dict,
    pages_available: bool,
) -> list[dict]:
    items = []

    def add(key, label, status, detail, rule, **kw):
        items.append(
            {"key": key, "label": label, "status": status, "detail": detail, "rule": rule, **kw}
        )

    if board:
        share = Decimal(board["independent_share"]) / 100
        add("independence", "Independent directors", "PASS" if share >= INDEPENDENT_MIN else "FLAG", f"{board['independent']} of {board['board_size']} directors are independent ({board['independent_share']}%).", "at least one third of the board is independent (SEBI minimum)", page=board["page"])  # fmt: skip
        add("women", "Woman director", "PASS" if board["woman_director_stated"] else "UNKNOWN", "the board sentence mentions a woman director" if board["woman_director_stated"] else "the board sentence does not mention a woman director", "at least one woman director", page=board["page"])  # fmt: skip
    else:
        reason = (
            "the annual report does not state board size and independent directors in one sentence"
            if pages_available
            else "no annual report with page text is stored"
        )
        add(
            "independence",
            "Independent directors",
            "UNKNOWN",
            reason,
            "at least one third of the board is independent (SEBI minimum)",
        )
    if comms and pages_available:
        missing = [c["committee"] for c in comms if not c["mentioned"]]
        add("committees", "Statutory committees disclosed", "PASS" if not missing else "FLAG", "all four committees are described in the report" if not missing else "not found in the report: " + ", ".join(missing), "the audit, nomination and remuneration, stakeholders relationship and risk management committees are named", pages=[p for c in comms for p in c["pages"][:1]])  # fmt: skip
    if promoter.get("available") and "change_pp" in promoter:
        change = Decimal(promoter["change_pp"])
        status = "FLAG" if change <= -PROMOTER_FALL_PP else "PASS"
        add("promoter_holding", "Promoter holding", status, f"promoter holding moved {change} percentage points between {promoter['from']} and {promoter['to']} (now {promoter['latest']['value']}%).", f"a fall of {PROMOTER_FALL_PP} points or more over the stored window is flagged", evidence_ids=promoter["change_evidence_ids"])  # fmt: skip
    else:
        add("promoter_holding", "Promoter holding", "UNKNOWN", promoter.get("reason", "fewer than two shareholding patterns are stored"), f"a fall of {PROMOTER_FALL_PP} points or more is flagged")  # fmt: skip
    add("promoter_pledge", "Promoter share pledges", "UNKNOWN", promoter.get("pledge", {}).get("reason", "not stored"), "pledged promoter shares are a governance risk")  # fmt: skip
    if auditor_flags is None:
        add("auditor", "Auditor's report", "UNKNOWN", "no stored auditor's report was read for this company", "no qualification, adverse opinion, disclaimer or going-concern paragraph")  # fmt: skip
    elif auditor_flags:
        add("auditor", "Auditor's report", "FLAG", "the auditor's report contains: " + "; ".join(str(f.get("label") if isinstance(f, dict) else f) for f in auditor_flags)[:300], "no qualification, adverse opinion, disclaimer or going-concern paragraph")  # fmt: skip
    else:
        add("auditor", "Auditor's report", "PASS", "no qualification, adverse opinion, disclaimer or going-concern paragraph was found", "no qualification, adverse opinion, disclaimer or going-concern paragraph")  # fmt: skip
    rpt = quotes.get("related_party") or []
    if rpt:
        arms = any(_arms_length(q["text"]) for q in rpt)
        add("related_party", "Related-party transactions", "PASS" if arms else "UNKNOWN", "the report states they are at arm's length / in the ordinary course of business" if arms else "the report discusses related-party transactions without that statement in the sentences read", "related-party transactions are stated to be at arm's length and in the ordinary course", page=rpt[0]["page"])  # fmt: skip
    else:
        add("related_party", "Related-party transactions", "UNKNOWN", "no related-party statement was found in the stored report", "related-party transactions are stated to be at arm's length and in the ordinary course")  # fmt: skip
    if alloc.get("available"):
        years = [y for y in alloc["years"] if y.get("returned_vs_fcf")]
        if years:
            exceed = [y["fy"] for y in years if y["returned_vs_fcf"] == "EXCEEDS_FCF"]
            add("returns_vs_fcf", "Shareholder returns against free cash flow", "FLAG" if exceed else "PASS", ("dividends and buybacks exceeded free cash flow in " + ", ".join(exceed)) if exceed else "dividends and buybacks were covered by free cash flow in every year with the data", "dividends + buybacks do not exceed operating cash flow less capital expenditure", evidence_ids=[i for y in years for i in y["evidence_ids"]][:8])  # fmt: skip
    return items


# ---------------------------------------------------------------- assembling


def _report_pages(session: Session, company_id: uuid.UUID):
    found = pr._latest_annual_report(session, company_id)
    if found is None:
        return None, []
    doc, ext, n = found
    rows = session.execute(
        select(DocumentPage.page_number, DocumentPage.text)
        .where(DocumentPage.extraction_id == ext.id)
        .order_by(DocumentPage.page_number)
    ).all()
    return {"id": str(doc.id), "title": doc.title, "period": doc.financial_period, "pages_read": n}, [(p, t) for p, t in rows]  # fmt: skip


def build(
    session: Session,
    company_id: uuid.UUID,
    store,
    basis: str,
    analysis: dict,
    auditor_flags=None,
    today: date | None = None,
) -> dict:
    doc, pages = _report_pages(session, company_id)
    quotes = extract(pages) if pages else {k: [] for k in TOPICS}
    board = parse_board(pages) if pages else None
    comms = committees(pages) if pages else []
    promoter = promoter_holding(store)
    alloc = allocation(store, basis)
    history = dividend_history(session, company_id)
    cx = capex_and_acquisitions(store, basis, analysis)
    leaders = ((analysis.get("profile") or {}).get("narrative") or {}).get("leaders") or []
    items = checklist(
        promoter=promoter,
        board=board,
        comms=comms,
        quotes=quotes,
        auditor_flags=auditor_flags,
        alloc=alloc,
        pages_available=bool(pages),
    )
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("PASS", "FLAG", "UNKNOWN")}
    mgmt_block = analysis.get("management") or {}
    available = bool(promoter.get("available") or alloc.get("available") or history.get("available") or cx.get("available") or any(quotes.values()) or leaders or board)  # fmt: skip
    return {
        "version": VERSION,
        "available": available,
        "basis": basis,
        "document": doc,
        "management": {
            "leaders": leaders,
            "background": quotes["background"],
            "tenure": quotes["tenure"],
            "track_record": track_record(analysis),
            "commentary_available": bool(mgmt_block.get("available")),
        },  # fmt: skip
        "promoter": promoter,
        "board": {"parsed": board, "committees": comms, "quotes": quotes["board"]},
        "related_party": quotes["related_party"],
        "governance_practices": quotes["governance"],
        "checklist": {"items": items, "counts": counts},
        "capital_allocation": {
            "policy": quotes["capital_allocation"],
            "uses_of_cash": alloc,
            "dividends_and_buybacks": history,
            "stated": quotes["dividend_buyback"],
        },  # fmt: skip
        "investment": {"assessment": cx, "stated": quotes["acquisitions"]},
        "definitions": [
            "DOCUMENT_QUOTE entries are sentences quoted verbatim from the company's latest stored annual report with their pages; they are the company's own statements and are not verified or paraphrased by EquityOS. PARSED values are read from one such sentence by a stated pattern and shown with it.",
            "The checklist has no composite score: each test is PASS, FLAG or UNKNOWN with its rule. UNKNOWN means the stored documents do not show it.",
            "Management background and tenure are only what the report states; no biography is added. The track record is the company's own financial record over the stored years, not a measure of any individual.",
            "Dividend announcements come from the exchange feed and cover a limited window; uses of cash come from the cash flow statement lines as printed.",
        ],
    }


def track_record(analysis: dict) -> dict:
    """The company's financial record over the stored years: what the management team has delivered."""
    prof = analysis.get("profitability") or {}
    health = analysis.get("health") or {}
    items = []
    for key, label in (("revenue", "Revenue"), ("net_profit", "Net profit")):
        g = (prof.get("growth") or {}).get(key) or {}
        c = g.get("cagr")
        if c:
            items.append(
                {
                    "label": f"{label} CAGR, {c['start_fy']} to {c['end_fy']}",
                    "value": f"{c['pct']}%",
                    "evidence_ids": c["evidence_ids"],
                }
            )
    roe = ((health.get("returns") or {}).get("metrics") or {}).get("roe") or {}
    if roe.get("available") and roe["series"]:
        a, b = roe["series"][0], roe["series"][-1]
        items.append({"label": f"Return on equity, {a['fy']} to {b['fy']}", "value": f"{Decimal(a['value'])}% to {Decimal(b['value'])}%", "evidence_ids": [a["evidence_id"], b["evidence_id"]]})  # fmt: skip
    return {"available": bool(items), "items": items, "note": "the company's record over the years stored, regardless of who led it; leadership changes are not dated here"} if items else {"available": False, "reason": "no growth or return record is stored for this company"}  # fmt: skip
