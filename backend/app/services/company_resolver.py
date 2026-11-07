"""Deterministic company resolver. No LLM is involved anywhere in this module.

Resolution runs in strict priority order and the FIRST stage that finds anything wins:

    1. ISIN                 (exact, check digit validated)
    2. BSE security code    (exact, 6 digits)
    3. Ticker / symbol      (exact, case-insensitive; NSE symbol or BSE scrip ID)
    4. Company name / alias (exact after normalisation)
    5. Fuzzy suggestions    (only when nothing above matched; never auto-selected)

Outcomes:
    RESOLVED     exactly one company matched in an exact stage
    AMBIGUOUS    several distinct companies matched in an exact stage; the user must choose
    SUGGESTIONS  nothing matched exactly, but similar companies exist; the user must choose
    NOT_FOUND    nothing matched
A company listed on both NSE and BSE is ONE company, so a ticker found on both is not ambiguous.
"""

import enum
import re
import uuid
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from app.core.identifiers import is_valid_isin
from app.core.text import normalize_text
from app.models import AliasType
from app.repositories import company_repository as repo
from app.services.search_index import get_search_index

FUZZY_MIN_SCORE = 60.0
SYMBOL_MIN_SCORE = 75.0  # tickers are short, so partial-match scoring is too noisy for them
PREFIX_SCORE = 95.0
MIN_FUZZY_QUERY_LENGTH = 2  # one character matches everything; not a useful suggestion
MAX_SUGGESTIONS = 5
MAX_QUERY_LENGTH = 100

_ISIN_SHAPE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_BSE_CODE = re.compile(r"^[0-9]{6}$")


class ResolutionStatus(enum.StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    SUGGESTIONS = "SUGGESTIONS"
    NOT_FOUND = "NOT_FOUND"


class MatchType(enum.StrEnum):
    ISIN = "ISIN"
    BSE_CODE = "BSE_CODE"
    TICKER = "TICKER"
    NAME = "NAME"
    ALIAS = "ALIAS"
    FUZZY = "FUZZY"


@dataclass(frozen=True)
class Candidate:
    company_id: uuid.UUID
    match_type: MatchType
    score: float  # 100.0 for every exact match; similarity score for FUZZY
    matched_on: str  # the identifier/name that matched, for display


@dataclass
class Resolution:
    query: str
    status: ResolutionStatus
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def match_type(self) -> MatchType | None:
        return self.candidates[0].match_type if self.candidates else None


def _exact(ids: list[uuid.UUID], match_type: MatchType, matched_on: str) -> list[Candidate]:
    return [Candidate(i, match_type, 100.0, matched_on) for i in dict.fromkeys(ids)]


def _finish(query: str, candidates: list[Candidate], notes: list[str]) -> Resolution:
    status = ResolutionStatus.RESOLVED if len(candidates) == 1 else ResolutionStatus.AMBIGUOUS
    return Resolution(query, status, candidates, notes)


def resolve_company(session: Session, query: str) -> Resolution:
    raw = query.strip()
    notes: list[str] = []
    if not raw or len(raw) > MAX_QUERY_LENGTH:
        return Resolution(query, ResolutionStatus.NOT_FOUND, notes=["Empty or oversized query."])

    upper = raw.upper()

    # 1. ISIN
    if _ISIN_SHAPE.match(upper):
        if is_valid_isin(upper):
            found = _exact(repo.find_company_ids_by_isin(session, upper), MatchType.ISIN, upper)
            if found:
                return _finish(query, found, notes)
        else:
            # Fuzzy-matching a code against names and tickers only produces noise, so stop here.
            notes.append(
                "Input looks like an ISIN but fails the ISIN check digit; it may be mistyped."
            )
            return Resolution(query, ResolutionStatus.NOT_FOUND, [], notes)

    # 2. BSE security code
    if _BSE_CODE.match(raw):
        found = _exact(
            repo.find_company_ids_by_security_code(session, raw), MatchType.BSE_CODE, raw
        )
        if found:
            return _finish(query, found, notes)

    # 3. Ticker / symbol
    found = _exact(repo.find_company_ids_by_symbol(session, upper), MatchType.TICKER, upper)
    if found:
        return _finish(query, found, notes)

    # 4. Exact company name or alias (after normalisation)
    normalized = normalize_text(raw)
    if normalized:
        hits = repo.find_aliases_by_normalized(session, normalized)
        name_types = (AliasType.LEGAL_NAME, AliasType.COMMON_NAME)
        found = []
        for company_id, alias_type, alias in hits:
            kind = MatchType.NAME if alias_type in name_types else MatchType.ALIAS
            found.append(Candidate(company_id, kind, 100.0, alias))
        found = list({c.company_id: c for c in found}.values())
        if found:
            return _finish(query, found, notes)

    # 5. Fuzzy suggestions (never auto-selected)
    suggestions = _fuzzy_suggestions(session, normalized)
    if suggestions:
        return Resolution(query, ResolutionStatus.SUGGESTIONS, suggestions, notes)
    return Resolution(query, ResolutionStatus.NOT_FOUND, [], notes)


def _fuzzy_suggestions(session: Session, normalized: str) -> list[Candidate]:
    if len(normalized) < MIN_FUZZY_QUERY_LENGTH:
        return []
    index = get_search_index(session)
    best: dict[uuid.UUID, Candidate] = {}

    def consider(company_id: uuid.UUID, display: str, score: float) -> None:
        current = best.get(company_id)
        if current is None or score > current.score:
            best[company_id] = Candidate(
                company_id, MatchType.FUZZY, round(float(score), 1), display
            )

    # Names: WRatio blends several fuzzy strategies and tolerates typos and word order.
    for _, score, i in process.extract(
        normalized, index.name_texts, scorer=fuzz.WRatio, score_cutoff=FUZZY_MIN_SCORE, limit=60
    ):
        consider(index.name_ids[i], index.name_displays[i], score)
    # Tickers are short, so partial-match scoring is noisy: use plain similarity.
    for _, score, i in process.extract(
        normalized, index.symbol_texts, scorer=fuzz.ratio, score_cutoff=SYMBOL_MIN_SCORE, limit=20
    ):
        consider(index.symbol_ids[i], index.symbol_displays[i], score)
    # A prefix ("reli" -> Reliance) is a strong signal for search-as-you-type: give it a floor.
    for i, text in enumerate(index.name_texts):
        if text.startswith(normalized):
            consider(index.name_ids[i], index.name_displays[i], PREFIX_SCORE)
    for i, text in enumerate(index.symbol_texts):
        if text.startswith(normalized):
            consider(index.symbol_ids[i], index.symbol_displays[i], PREFIX_SCORE)

    ranked = sorted(best.values(), key=lambda c: (-c.score, len(c.matched_on), str(c.company_id)))
    return ranked[:MAX_SUGGESTIONS]
