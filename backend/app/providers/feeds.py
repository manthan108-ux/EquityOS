"""RSS/Atom feed providers: RBI, SEBI (official, regulator tier) and company news (secondary).

A feed item is a *pointer* (headline, link, date), not proof of anything. News providers return
headlines and links only; they never summarise an article they have not read, and a headline alone
is never treated as an investment conclusion (spec section 14).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import feedparser

from app.acquisition.http import FetchResult, PoliteClient
from app.core.text import normalize_text

FEED_TTL = timedelta(minutes=30)
_TAGS = re.compile(r"<[^>]+>")
MAX_SUMMARY = 500


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    published: datetime | None
    summary: str
    publisher: str | None
    source_code: str
    retrieved_at: datetime | None


@dataclass
class FeedResult:
    fetch: FetchResult
    items: list[FeedItem]

    @property
    def ok(self) -> bool:
        return self.fetch.ok


def validate_feed(body: bytes, _ctype: str | None) -> str | None:
    head = body[:2000].lower()
    return (
        None
        if (b"<rss" in head or b"<feed" in head or b"<?xml" in head)
        else "not an RSS/Atom feed"
    )


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=UTC) if parsed else None


def parse_feed(body: bytes, source_code: str, retrieved_at: datetime | None) -> list[FeedItem]:
    items = []
    for entry in feedparser.parse(body).entries:
        title = _TAGS.sub("", entry.get("title", "")).strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue  # an item we cannot cite is not usable
        summary = " ".join(_TAGS.sub(" ", entry.get("summary", "")).split())[:MAX_SUMMARY]
        publisher = (entry.get("source") or {}).get("title")
        items.append(
            FeedItem(title, link, _published(entry), summary, publisher, source_code, retrieved_at)
        )
    return items


def dedupe(items: list[FeedItem]) -> list[FeedItem]:
    """Drop repeats of the same story (same normalised headline), keeping the earliest item."""
    seen: dict[str, FeedItem] = {}
    for item in sorted(items, key=lambda i: i.published or datetime.max.replace(tzinfo=UTC)):
        key = normalize_text(re.sub(r"\s[-|]\s[^-|]+$", "", item.title))  # strip " - Publisher"
        seen.setdefault(key, item)
    return sorted(
        seen.values(), key=lambda i: i.published or datetime.min.replace(tzinfo=UTC), reverse=True
    )


class _FeedProvider:
    source_code: str
    url: str

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def latest(self) -> FeedResult:
        fetch = self._client.fetch(
            self.url, source=self.source_code, ttl=FEED_TTL, validate=validate_feed
        )
        items = parse_feed(fetch.content, self.source_code, fetch.retrieved_at) if fetch.ok else []
        return FeedResult(fetch, items)


class RbiPressReleases(_FeedProvider):
    source_code = "RBI_RSS"
    url = "https://www.rbi.org.in/pressreleases_rss.xml"


class SebiAnnouncements(_FeedProvider):
    source_code = "SEBI_RSS"
    url = "https://www.sebi.gov.in/sebirss.xml"


# Publisher feeds that are reachable AND permitted by their robots.txt (checked 2026-11-07).
# Google News RSS search, Business Standard and PIB were tried and are excluded: robots.txt
# disallows the first and the others answer HTTP 403, which we do not bypass.
NEWS_FEEDS = [
    ("MINT_RSS", "https://www.livemint.com/rss/companies"),
    ("MINT_RSS", "https://www.livemint.com/rss/markets"),
    ("ET_RSS", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("MONEYCONTROL_RSS", "https://www.moneycontrol.com/rss/business.xml"),
    ("BUSINESSLINE_RSS", "https://www.thehindubusinessline.com/companies/feeder/default.rss"),
    ("BUSINESSLINE_RSS", "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
]
MIN_NAME_LENGTH = 3


@dataclass
class NewsResult:
    items: list[FeedItem]
    fetches: list[FetchResult]  # one per feed, so failures stay visible

    @property
    def feeds_ok(self) -> int:
        return sum(1 for f in self.fetches if f.ok)


def mentions(item: FeedItem, names: list[str]) -> bool:
    """True if any name appears as a whole phrase in the headline or summary."""
    haystack = f" {normalize_text(item.title + ' ' + item.summary)} "
    return any(
        f" {n} " in haystack
        for n in (normalize_text(x) for x in names)
        if len(n) >= MIN_NAME_LENGTH
    )


class CompanyNewsProvider:
    """Recent headlines mentioning a company, from permitted publisher RSS feeds (tier 5).

    Feeds only hold recent items, so this is a window of the latest news, not history. Matching is
    a local whole-phrase check on headline/summary: an item is a lead to read, not a conclusion.
    """

    def __init__(self, client: PoliteClient) -> None:
        self._client = client

    def headlines(self, names: list[str]) -> NewsResult:
        items: list[FeedItem] = []
        fetches: list[FetchResult] = []
        for source_code, url in NEWS_FEEDS:
            fetch = self._client.fetch(
                url, source=source_code, ttl=FEED_TTL, validate=validate_feed
            )
            fetches.append(fetch)
            if fetch.ok:
                parsed = parse_feed(fetch.content, source_code, fetch.retrieved_at)
                items += [i for i in parsed if mentions(i, names)]
        return NewsResult(dedupe(items), fetches)
