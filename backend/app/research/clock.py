"""The research workflow's notion of 'today'. One place, so tests can fix the date and so every
staleness rule (annual figures, quarters, prices) is judged against the same day."""

from datetime import date


def today() -> date:
    return date.today()
