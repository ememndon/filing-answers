"""Fetching annual reports from the SEC's EDGAR service.

EDGAR is free, complete and needs no account, which makes it the right
source for a project anyone should be able to run. It asks three things
in return, and all three are enforced rather than requested:

  - every request carries a contact address, so they can reach you
  - no more than ten requests a second
  - you do not re-download what you already have

The third is not in their guidance, it is just manners. A 10-K runs to
several megabytes, and a program that fetches the same one on every run
is being rude at someone else's expense — so documents are cached on
disk and the network is touched once.

Reference: https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

TICKER_INDEX_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

#: The SEC's published ceiling is ten requests a second. Running at the
#: limit invites being throttled by a service with no obligation to be
#: forgiving, so this sits comfortably underneath it.
MIN_SECONDS_BETWEEN_REQUESTS = 0.15

#: Forms that are an annual report. 10-K is the ordinary one; the others
#: are the same filing from a smaller company or a transition period, and
#: a company that filed one of those did not also file a plain 10-K.
ANNUAL_FORMS = ("10-K", "10-K405", "10-KSB", "10-KT")


class FilingNotFoundError(Exception):
    """No filing of the requested kind exists for this company."""


class UnknownTickerError(Exception):
    """The ticker is not in the SEC's index of registered companies."""


class Filing(BaseModel):
    """One filing, identified well enough to fetch and to cite."""

    cik: str = Field(description="Ten-digit zero-padded SEC identifier")
    ticker: str
    company: str
    form: str
    filed: date
    period: date | None = Field(
        default=None,
        description="End of the fiscal period covered, which is what a reader means by the year",
    )
    accession: str = Field(description="Accession number, dashes removed")
    document: str = Field(description="Filename of the primary document within the filing")

    @property
    def url(self) -> str:
        # The archive path drops the zero padding the submissions API requires.
        return ARCHIVE_URL.format(
            cik=self.cik.lstrip("0"),
            accession=self.accession,
            document=self.document,
        )

    @property
    def label(self) -> str:
        """How this filing should be referred to in an answer."""
        year = (self.period or self.filed).year
        return f"{self.ticker} {self.form} FY{year}"


class _Throttle:
    """Spaces requests out, across threads, without a scheduler."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self._min_interval:
                time.sleep(self._min_interval - gap)
            self._last = time.monotonic()


class EdgarClient:
    """Reads company filings from EDGAR, politely.

    The HTTP client is injectable so the behaviour above can be tested
    without touching the SEC: their servers are not a test fixture, and a
    suite that depends on them fails on a train.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        cache_dir: Path | str = "data/filings",
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError("EDGAR requires a contact email address in the User-Agent")
        self._cache = Path(cache_dir)
        self._throttle = _Throttle(MIN_SECONDS_BETWEEN_REQUESTS)
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        # Held here and applied per request rather than baked into the
        # client, so the contact address travels with the fetcher. Setting
        # it on a client we construct would mean a caller who passes their
        # own client silently sends requests EDGAR refuses.
        self._headers = {
            "User-Agent": user_agent,
            # EDGAR serves gzip and says so; asking for it cuts a 10-K from
            # megabytes to something reasonable.
            "Accept-Encoding": "gzip, deflate",
        }
        self._ticker_index: dict[str, tuple[str, str]] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the network -----------------------------------------------------

    def _get(self, url: str) -> httpx.Response:
        self._throttle.wait()
        response = self._client.get(url, headers=self._headers)
        response.raise_for_status()
        return response

    # -- ticker -> company -----------------------------------------------

    def _load_ticker_index(self) -> dict[str, tuple[str, str]]:
        """Every registered ticker, mapped to its identifier and name.

        One file covers the whole market, so it is fetched once per
        process rather than per lookup.
        """
        if self._ticker_index is not None:
            return self._ticker_index

        raw = self._get(TICKER_INDEX_URL).json()
        # The file is a JSON object keyed by row number, not a list.
        index: dict[str, tuple[str, str]] = {}
        for row in raw.values():
            ticker = str(row["ticker"]).upper()
            index[ticker] = (f"{int(row['cik_str']):010d}", str(row["title"]))
        self._ticker_index = index
        return index

    def cik_for(self, ticker: str) -> tuple[str, str]:
        """The SEC identifier and registered name for a ticker."""
        index = self._load_ticker_index()
        key = ticker.strip().upper()
        if key not in index:
            raise UnknownTickerError(f"{ticker!r} is not a ticker registered with the SEC")
        return index[key]

    # -- company -> filing -----------------------------------------------

    def latest_annual_report(self, ticker: str) -> Filing:
        """The most recent annual report this company filed."""
        cik, company = self.cik_for(ticker)
        submissions = self._get(SUBMISSIONS_URL.format(cik=cik)).json()
        recent = submissions.get("filings", {}).get("recent", {})

        forms = recent.get("form", [])
        if not forms:
            raise FilingNotFoundError(f"{ticker} has no filings on record")

        # The arrays run in parallel and arrive newest first, so the first
        # annual form encountered is the most recent one.
        for i, form in enumerate(forms):
            if form not in ANNUAL_FORMS:
                continue
            period_raw = recent["reportDate"][i]
            return Filing(
                cik=cik,
                ticker=ticker.strip().upper(),
                company=company,
                form=form,
                filed=date.fromisoformat(recent["filingDate"][i]),
                period=date.fromisoformat(period_raw) if period_raw else None,
                accession=recent["accessionNumber"][i].replace("-", ""),
                document=recent["primaryDocument"][i],
            )

        raise FilingNotFoundError(f"{ticker} has no annual report among its recent filings")

    # -- filing -> document ----------------------------------------------

    def document_html(self, filing: Filing) -> str:
        """The filing itself, from disk if it has been fetched before."""
        cached = self._cache / f"{filing.accession}-{filing.document}"
        if cached.exists():
            return cached.read_text(encoding="utf-8", errors="replace")

        html = self._get(filing.url).text
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(html, encoding="utf-8")
        return html


def cached_filings(cache_dir: Path | str = "data/filings") -> list[Path]:
    """Filings already on disk, for working offline."""
    directory = Path(cache_dir)
    return sorted(directory.glob("*")) if directory.exists() else []


def looks_like_annual_report(html: str) -> bool:
    """A cheap check that a fetched page is the filing and not an error.

    EDGAR answers some bad requests with a courteous HTML page rather
    than a status code, so a fetch can appear to succeed and hand back a
    document explaining that it does not exist.
    """
    if len(html) < 10_000:
        return False

    # An error page announces itself immediately, and is short enough that
    # the head is all of it.
    head = html[:20_000].lower()
    if "the page you requested" in head or "no matching" in head:
        return False

    # The whole document, not the head. A filing today is Inline XBRL: it
    # opens with an XML preamble and tens of thousands of characters of
    # tagging and style definitions before a word of English appears. The
    # first version of this looked only at the opening 20,000 characters
    # and rejected a perfectly good Apple 10-K, because at that point the
    # document had not yet said what it was.
    return bool(re.search(r"annual report|form\s*10-k", html.lower()))
