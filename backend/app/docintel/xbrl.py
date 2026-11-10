"""Offline reader for XBRL instance documents (XBRL 2.1, as filed on the exchanges).

Reads a LOCAL file. It never fetches anything (no schema, no taxonomy, no network): the instance is
self-describing enough for facts, contexts, dimensions and units. Raw content is preserved exactly:
every fact keeps its concept, context, dimensions, unit, decimals and text. Nothing here decides
what a concept MEANS for EquityOS - that is `xbrl_map.py`.

Safety: the instance is untrusted input. Documents with a DOCTYPE or any entity declaration are
refused outright (an XBRL instance never needs one), which closes entity-expansion and external
entity attacks without a third-party parser. Size is capped.

Not supported (refused with a clear message, never half-read): inline XBRL (iXBRL, an HTML file
with ix: tags) and anything that is not an XBRL 2.1 `xbrl` root element.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

XBRLI = "http://www.xbrl.org/2003/instance"
LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
MAX_BYTES = 60 * 1024 * 1024
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class XbrlError(ValueError):
    """The file cannot be read as an XBRL instance (the message says why)."""


@dataclass(frozen=True)
class Context:
    id: str
    entity_scheme: str | None
    entity_id: str | None
    start: date | None
    end: date | None  # duration end
    instant: date | None
    # ((axis qname, member qname or typed-member text, "explicit" | "typed"), ...), sorted
    dimensions: tuple[tuple[str, str, str], ...] = ()

    @property
    def is_instant(self) -> bool:
        return self.instant is not None

    @property
    def period_end(self) -> date | None:
        return self.instant or self.end

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "entity": [self.entity_scheme, self.entity_id],
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "instant": self.instant.isoformat() if self.instant else None,
            "dimensions": [list(d) for d in self.dimensions],
        }


@dataclass(frozen=True)
class Unit:
    id: str
    measures: tuple[str, ...]  # numerator measures
    denominators: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        top = "*".join(m.split(":")[-1] for m in self.measures)
        if self.denominators:
            return top + "/" + "*".join(m.split(":")[-1] for m in self.denominators)
        return top


@dataclass(frozen=True)
class Fact:
    index: int  # position in the document
    namespace: str
    local: str
    prefix: str | None
    context_ref: str
    unit_ref: str | None
    decimals: str | None
    precision: str | None
    text: str  # exactly as filed (stripped of surrounding whitespace)
    nil: bool

    @property
    def qname(self) -> str:
        return f"{self.prefix}:{self.local}" if self.prefix else self.local

    @property
    def numeric(self) -> Decimal | None:
        """The value as a Decimal, or None for text facts, nil facts and unparseable numbers."""
        if self.nil or self.unit_ref is None:
            return None
        try:
            value = Decimal(self.text.strip())
        except InvalidOperation:
            return None
        return value if value.is_finite() else None


@dataclass
class Instance:
    namespaces: dict[str, str]  # prefix -> uri
    schema_refs: list[str]
    contexts: dict[str, Context]
    units: dict[str, Unit]
    facts: list[Fact] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # defects found while reading (facts kept)

    def context(self, fact: Fact) -> Context | None:
        return self.contexts.get(fact.context_ref)

    def unit(self, fact: Fact) -> Unit | None:
        return self.units.get(fact.unit_ref) if fact.unit_ref else None


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _date(text: str | None, what: str, problems: list[str]) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        problems.append(f"{what}: {text!r} is not a date")
        return None


def _qname(text: str, ns_by_prefix: dict[str, str]) -> str:
    return text.strip()  # kept as filed ("in-capmkt:ConsolidatedMember"); the prefix map is stored


def _context(el: ET.Element, ns_by_prefix: dict[str, str], problems: list[str]) -> Context | None:
    cid = el.get("id")
    if not cid:
        problems.append("a context without an id was ignored")
        return None
    entity = el.find(f"{{{XBRLI}}}entity")
    scheme = ident = None
    dims: list[tuple[str, str, str]] = []
    if entity is not None:
        ident_el = entity.find(f"{{{XBRLI}}}identifier")
        if ident_el is not None:
            scheme, ident = ident_el.get("scheme"), (ident_el.text or "").strip() or None
        seg = entity.find(f"{{{XBRLI}}}segment")
        if seg is not None:
            for m in seg:
                ns, local = _split(m.tag)
                if ns == XBRLDI and local == "explicitMember":
                    dims.append(
                        (m.get("dimension", ""), _qname(m.text or "", ns_by_prefix), "explicit")
                    )
                elif ns == XBRLDI and local == "typedMember":
                    inner = next(iter(m), None)
                    value = (inner.text or "").strip() if inner is not None else ""
                    dims.append((m.get("dimension", ""), value, "typed"))
    period = el.find(f"{{{XBRLI}}}period")
    start = end = instant = None
    if period is None:
        problems.append(f"context {cid}: no period")
    else:
        start = _date(_t(period, "startDate"), f"context {cid} startDate", problems)
        end = _date(_t(period, "endDate"), f"context {cid} endDate", problems)
        instant = _date(_t(period, "instant"), f"context {cid} instant", problems)
        if instant is None and (start is None or end is None):
            problems.append(f"context {cid}: neither an instant nor a complete duration")
        if start and end and start > end:
            problems.append(f"context {cid}: starts after it ends")
    return Context(cid, scheme, ident, start, end, instant, tuple(sorted(dims)))


def _t(parent: ET.Element, local: str) -> str | None:
    el = parent.find(f"{{{XBRLI}}}{local}")
    return el.text if el is not None else None


def _unit(el: ET.Element) -> Unit | None:
    uid = el.get("id")
    if not uid:
        return None
    measures = tuple(
        (m.text or "").strip() for m in el.findall(f"{{{XBRLI}}}measure") if (m.text or "").strip()
    )
    divide = el.find(f"{{{XBRLI}}}divide")
    if divide is not None:
        num = divide.find(f"{{{XBRLI}}}unitNumerator")
        den = divide.find(f"{{{XBRLI}}}unitDenominator")
        top = tuple(
            (m.text or "").strip()
            for m in (num if num is not None else [])
            if (m.text or "").strip()
        )
        bottom = tuple(
            (m.text or "").strip()
            for m in (den if den is not None else [])
            if (m.text or "").strip()
        )
        return Unit(uid, top, bottom)
    return Unit(uid, measures)


def parse(data: bytes) -> Instance:
    """Read an XBRL 2.1 instance from bytes. Raises XbrlError if it is not one."""
    if len(data) > MAX_BYTES:
        raise XbrlError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if _DTD.search(data):
        raise XbrlError("a DOCTYPE or ENTITY declaration is not allowed (refused for safety)")
    prefixes: dict[str, str] = {}
    root = None
    try:
        for event, node in ET.iterparse(_Bytes(data), events=("start-ns", "end")):
            if event == "start-ns":
                prefixes.setdefault(node[0], node[1])
            else:
                root = node  # the root element ends last
    except ET.ParseError as exc:
        raise XbrlError(f"not well-formed XML: {exc}") from exc
    if root is None:
        raise XbrlError("empty document")
    ns, local = _split(root.tag)
    if (
        local.lower() == "html"
        or root.find(".//{http://www.xbrl.org/2013/inlineXBRL}*") is not None
    ):
        raise XbrlError("inline XBRL (iXBRL) is not supported; supply the XBRL instance (.xml)")
    if (ns, local) != (XBRLI, "xbrl"):
        raise XbrlError(f"root element is {{{ns}}}{local}, not an XBRL 2.1 instance")

    inst = Instance(prefixes, [], {}, {})
    uri_to_prefix = {}
    for p, u in prefixes.items():
        uri_to_prefix.setdefault(u, p)
    for child in root:
        cns, clocal = _split(child.tag)
        if (cns, clocal) == (LINK, "schemaRef"):
            href = child.get(f"{{{XLINK}}}href")
            if href:
                inst.schema_refs.append(href)
        elif (cns, clocal) == (XBRLI, "context"):
            ctx = _context(child, prefixes, inst.problems)
            if ctx is not None:
                if ctx.id in inst.contexts:
                    inst.problems.append(f"context {ctx.id} is defined twice")
                inst.contexts[ctx.id] = ctx
        elif (cns, clocal) == (XBRLI, "unit"):
            unit = _unit(child)
            if unit is not None:
                inst.units[unit.id] = unit
    for i, child in enumerate(root):
        cns, clocal = _split(child.tag)
        if cns in (XBRLI, LINK) or not isinstance(child.tag, str):
            continue
        cref = child.get("contextRef")
        if cref is None:
            continue  # not an item (e.g. a footnote link or a tuple wrapper)
        nil = (child.get(f"{{{XSI}}}nil") or "").lower() in ("true", "1")
        inst.facts.append(
            Fact(
                index=i,
                namespace=cns,
                local=clocal,
                prefix=uri_to_prefix.get(cns),
                context_ref=cref,
                unit_ref=child.get("unitRef"),
                decimals=child.get("decimals"),
                precision=child.get("precision"),
                text=("".join(child.itertext())).strip(),
                nil=nil,
            )
        )
    for f in inst.facts:
        if f.context_ref not in inst.contexts:
            inst.problems.append(f"{f.qname}: context {f.context_ref} is not defined")
        if f.unit_ref and f.unit_ref not in inst.units:
            inst.problems.append(f"{f.qname}: unit {f.unit_ref} is not defined")
    return inst


class _Bytes:
    """A minimal file-like wrapper so `iterparse` can read bytes without touching the disk."""

    def __init__(self, data: bytes) -> None:
        self._data, self._pos = data, 0

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk
