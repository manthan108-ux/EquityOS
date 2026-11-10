"""Research preferences: which accounting basis, which reporting period, which extras.

The defaults are the ones the product asked for: CONSOLIDATED figures, the LATEST AVAILABLE QUARTER,
optional items OFF. A preference is a promise to the user:

* the BASIS is never switched silently. If the requested basis is unavailable for a figure, that
  figure is reported as missing, with the reason and the fact that the other basis exists, and the
  user decides whether to change the preference;
* a period chosen for a session (a fiscal year or a quarter) is strict: it is not replaced by a
  newer one, and it is kept with the session (and in the session's preference log) so a saved
  session can be reopened as it was researched;
* "latest quarter" may fall back to the latest annual figure only when no recent quarter exists for
  that figure, and the fallback is shown beside the figure.
"""

from dataclasses import asdict, dataclass
from datetime import date

BASES = ("CONSOLIDATED", "STANDALONE")
PERIOD_MODES = ("LATEST_QUARTER", "LATEST_ANNUAL", "QUARTER", "FISCAL_YEAR")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
# (month, day, calendar year offset from the fiscal year): Q1 ends 30 Jun of the year before FY-end
_QUARTER_END = {"Q1": (6, 30, -1), "Q2": (9, 30, -1), "Q3": (12, 31, -1), "Q4": (3, 31, 0)}


class PreferenceError(ValueError):
    pass


@dataclass(frozen=True)
class Preferences:
    basis: str = "CONSOLIDATED"
    period_mode: str = "LATEST_QUARTER"
    fiscal_year: int | None = None
    quarter: str | None = None
    include_optional: bool = False

    def __post_init__(self) -> None:
        if self.basis not in BASES:
            raise PreferenceError(f"basis must be one of {', '.join(BASES)}")
        if self.period_mode not in PERIOD_MODES:
            raise PreferenceError(f"period_mode must be one of {', '.join(PERIOD_MODES)}")
        if self.period_mode in ("QUARTER", "FISCAL_YEAR"):
            if self.fiscal_year is None or not 2000 <= self.fiscal_year <= 2100:
                raise PreferenceError("a fiscal year such as 2027 is needed for this period mode")
        if self.period_mode == "QUARTER" and self.quarter not in QUARTERS:
            raise PreferenceError("a quarter (Q1-Q4 of the fiscal year) is needed")
        if self.period_mode in ("LATEST_QUARTER", "LATEST_ANNUAL") and (
            self.fiscal_year is not None or self.quarter is not None
        ):
            raise PreferenceError("a fiscal year or quarter applies only to a chosen period")

    @property
    def strict_period(self) -> bool:
        """A chosen period is never replaced by another one."""
        return self.period_mode in ("QUARTER", "FISCAL_YEAR")

    def target_end(self) -> date | None:
        """The period end a chosen period refers to (None for the 'latest' modes)."""
        if self.period_mode == "FISCAL_YEAR":
            return date(self.fiscal_year, 3, 31)
        if self.period_mode == "QUARTER":
            month, day, offset = _QUARTER_END[self.quarter]
            return date(self.fiscal_year + offset, month, day)
        return None

    def period_label(self) -> str:
        if self.period_mode == "FISCAL_YEAR":
            return f"FY{self.fiscal_year} (year ended 31 Mar {self.fiscal_year})"
        if self.period_mode == "QUARTER":
            end = self.target_end()
            return f"{self.quarter} FY{self.fiscal_year} (quarter ended {end:%d %b %Y})"
        if self.period_mode == "LATEST_ANNUAL":
            return "latest available financial year"
        return "latest available quarter"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Preferences":
        data = dict(data or {})
        unknown = set(data) - {"basis", "period_mode", "fiscal_year", "quarter", "include_optional"}
        if unknown:
            raise PreferenceError(f"unknown preference(s): {', '.join(sorted(unknown))}")
        if "fiscal_year" in data and data["fiscal_year"] is not None:
            try:
                data["fiscal_year"] = int(data["fiscal_year"])
            except (TypeError, ValueError) as exc:
                raise PreferenceError("fiscal_year must be a number") from exc
        if "include_optional" in data and not isinstance(data["include_optional"], bool):
            raise PreferenceError("include_optional must be true or false")
        return cls(**data)


DEFAULT = Preferences()
