"""In-memory index used for fuzzy company search.

Fuzzy matching must compare the query against EVERY alias and symbol. Reading them from the
database on each keystroke does not scale to the full NSE+BSE universe (thousands of companies),
so the index is built once and reused. It is rebuilt when it is older than the TTL or after a
master-data import calls `invalidate_search_index()`.
"""

import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.repositories import company_repository as repo

TTL_SECONDS = 300.0


@dataclass(frozen=True)
class SearchIndex:
    """Parallel lists so rapidfuzz can scan the `*_texts` lists at native speed."""

    name_texts: list[str]  # normalized aliases
    name_ids: list[uuid.UUID]
    name_displays: list[str]  # the alias as originally written
    symbol_texts: list[str]  # lower-case symbols
    symbol_ids: list[uuid.UUID]
    symbol_displays: list[str]


_lock = threading.Lock()
_cached: tuple[float, SearchIndex] | None = None


def build_search_index(session: Session) -> SearchIndex:
    aliases = repo.list_alias_index(session)
    symbols = repo.list_symbol_index(session)
    return SearchIndex(
        name_texts=[normalized for _, normalized, _ in aliases],
        name_ids=[company_id for company_id, _, _ in aliases],
        name_displays=[display for _, _, display in aliases],
        symbol_texts=[symbol.lower() for _, symbol in symbols],
        symbol_ids=[company_id for company_id, _ in symbols],
        symbol_displays=[symbol for _, symbol in symbols],
    )


def get_search_index(session: Session) -> SearchIndex:
    global _cached
    with _lock:
        if _cached is None or time.monotonic() - _cached[0] > TTL_SECONDS:
            _cached = (time.monotonic(), build_search_index(session))
        return _cached[1]


def invalidate_search_index() -> None:
    global _cached
    with _lock:
        _cached = None
