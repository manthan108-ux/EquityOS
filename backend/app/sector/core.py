"""Pure logic of the sector engine: who the peers are, where a company stands, how it scores.

No database and no I/O, so every rule can be tested with small hand-written cases.

What the numbers mean (stated so nobody over-reads them):

* A *rank* is the share of peers that the company beats on one metric, counting a tie as half. It is
  a relative standing among the chosen peers, not a measure of quality, and with few peers it is
  coarse: with 3 peers it can only be 0, 16.7, 33.3, ... 100.
* A *pillar score* is the plain average of the ranks of the metrics available in that pillar.
* The *overall score* is the weighted average of pillar scores using the sector's curated weights,
  renormalised over the pillars that have at least one metric. A pillar with no data is dropped
  (its weight goes to the others) and that is recorded as a weight adjustment, never hidden.
"""

import math
from dataclasses import dataclass, field
from decimal import Decimal

MAX_PEERS = 8
# An overall score is only published when the pillars that have data carry at least this share (%)
# of the sector's weight; below it the picture is too partial to summarise in one number.
MIN_WEIGHT_COVERAGE = Decimal(60)
HIGHER, LOWER = "HIGHER", "LOWER"
TWO_PLACES = Decimal("0.01")


@dataclass(frozen=True)
class Candidate:
    company_id: str
    name: str
    sector_code: str | None
    industry_code: str | None
    profile: str  # BANK | NONFIN
    size: Decimal | None  # latest annual total income, in rupees


@dataclass(frozen=True)
class PeerPick:
    candidate: Candidate
    tier: str  # SAME_INDUSTRY | SAME_SECTOR
    rank: int
    size_ratio: Decimal | None
    reason: str


def select_peers(
    subject: Candidate, pool: list[Candidate], max_peers: int = MAX_PEERS
) -> tuple[list[PeerPick], list[tuple[Candidate, str]]]:
    """Peers of `subject` among `pool`, and a reason for every candidate left out.

    A peer must have the same statement profile (a bank is never compared with a manufacturer) and
    the same sector. Same-industry peers come first, then same-sector ones; within a tier the
    closest in size (log distance of annual total income) first.
    """
    rejected: list[tuple[Candidate, str]] = []
    if not subject.sector_code:
        return [], [(c, "the company has no sector assigned") for c in pool if c != subject]
    scored: list[tuple[tuple, Candidate, str, Decimal | None]] = []
    for c in pool:
        if c.company_id == subject.company_id:
            continue
        if c.profile != subject.profile:
            rejected.append((c, f"different statement profile ({c.profile} vs {subject.profile})"))
            continue
        if not c.sector_code:
            rejected.append((c, "no sector assigned"))
            continue
        if c.sector_code != subject.sector_code:
            rejected.append((c, f"different sector ({c.sector_code})"))
            continue
        tier = (
            "SAME_INDUSTRY"
            if c.industry_code and c.industry_code == subject.industry_code
            else "SAME_SECTOR"
        )
        ratio = None
        distance = math.inf
        if c.size and subject.size and c.size > 0 and subject.size > 0:
            ratio = (c.size / subject.size).quantize(TWO_PLACES)
            distance = abs(math.log(float(c.size / subject.size)))
        scored.append(((0 if tier == "SAME_INDUSTRY" else 1, distance, c.name), c, tier, ratio))
    scored.sort(key=lambda t: t[0])
    picks = []
    for i, (_key, c, tier, ratio) in enumerate(scored[:max_peers], start=1):
        why = "same industry" if tier == "SAME_INDUSTRY" else "same sector, different industry"
        if ratio is not None:
            why += f"; {ratio}x the company's size (annual total income)"
        else:
            why += "; size unknown, so ranked last within its tier"
        picks.append(PeerPick(c, tier, i, ratio, why))
    for _key, c, _tier, _ratio in scored[max_peers:]:
        rejected.append((c, f"beyond the {max_peers} closest peers"))
    return picks, rejected


def median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def favourable_share(value: Decimal, peers: list[Decimal], direction: str) -> Decimal:
    """Share of peers (percent) the value beats; a tie counts half."""
    if not peers:
        raise ValueError("no peers to compare with")
    beaten = sum(1 for p in peers if (value > p if direction == HIGHER else value < p))
    ties = sum(1 for p in peers if value == p)
    return (Decimal(beaten) + Decimal(ties) / 2) / Decimal(len(peers)) * 100


def mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


@dataclass
class WeightChange:
    pillar: str
    base: Decimal
    adjusted: Decimal
    reason: str


@dataclass
class Scorecard:
    pillar_scores: dict[str, Decimal] = field(default_factory=dict)
    weights: dict[str, Decimal] = field(default_factory=dict)  # adjusted, sum to 100
    overall: Decimal | None = None
    coverage: Decimal = Decimal(0)  # share of the base weight that has data, in percent
    withheld: str | None = None  # why no overall score was produced
    changes: list[WeightChange] = field(default_factory=list)


def score(
    ranks_by_pillar: dict[str, list[Decimal]], base_weights: dict[str, int | Decimal]
) -> Scorecard:
    """Pillar scores and the weighted overall, renormalising over pillars that have data."""
    card = Scorecard()
    present = {p: r for p, r in ranks_by_pillar.items() if r}
    for pillar, ranks in present.items():
        card.pillar_scores[pillar] = mean(ranks)
    weight_present = sum((Decimal(base_weights[p]) for p in present), Decimal(0))
    total_weight = sum((Decimal(w) for w in base_weights.values()), Decimal(0))
    card.coverage = weight_present / total_weight * 100 if total_weight else Decimal(0)
    for pillar, base in base_weights.items():
        base = Decimal(base)
        if pillar in present and weight_present > 0:
            adjusted = base / weight_present * 100
            card.weights[pillar] = adjusted
            if adjusted != base:
                card.changes.append(
                    WeightChange(
                        pillar,
                        base,
                        adjusted,
                        "scaled up because pillars without usable data were dropped",
                    )
                )
        else:
            card.changes.append(
                WeightChange(pillar, base, Decimal(0), "no metric in this pillar could be compared")
            )
    if card.weights and card.coverage < MIN_WEIGHT_COVERAGE:
        card.withheld = (
            f"only {card.coverage:.0f}% of the sector's weight has data (minimum "
            f"{MIN_WEIGHT_COVERAGE:.0f}%): pillar scores are shown, no overall score"
        )
    elif card.weights:
        card.overall = sum(
            (card.pillar_scores[p] * w for p, w in card.weights.items()), Decimal(0)
        ) / Decimal(100)
    return card


__all__ = [
    "Candidate",
    "HIGHER",
    "LOWER",
    "MAX_PEERS",
    "MIN_WEIGHT_COVERAGE",
    "PeerPick",
    "Scorecard",
    "WeightChange",
    "favourable_share",
    "mean",
    "median",
    "score",
    "select_peers",
]
