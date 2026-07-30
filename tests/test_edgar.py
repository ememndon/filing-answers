"""Reading filings from EDGAR, without calling EDGAR.

Their servers are not a test fixture. A suite that depends on them is
slow, fails on a train, and puts load on a public service every time
somebody runs pytest — so the HTTP client is injected and the fixtures
below are trimmed copies of real EDGAR responses.

There is one live test, marked so it does not run by default:

    pytest -m live
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest

from filing_answers.edgar import (
    EdgarClient,
    Filing,
    FilingNotFoundError,
    UnknownTickerError,
    looks_like_annual_report,
)

TICKER_INDEX = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}

SUBMISSIONS = {
    "cik": "320193",
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            # newest first, as EDGAR returns them
            "form": ["8-K", "10-Q", "10-K", "10-K"],
            "filingDate": ["2025-01-15", "2024-11-01", "2024-11-01", "2023-11-03"],
            "reportDate": ["2025-01-15", "2024-09-28", "2024-09-28", "2023-09-30"],
            "accessionNumber": [
                "0000320193-25-000001",
                "0000320193-24-000122",
                "0000320193-24-000123",
                "0000320193-23-000106",
            ],
            "primaryDocument": ["a8k.htm", "aapl-20240928.htm", "aapl-20240928.htm", "aapl.htm"],
        }
    },
}


def fake_transport(routes: dict[str, object]) -> httpx.Client:
    """An HTTP client that answers from a dictionary of URLs."""

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, payload in routes.items():
            if fragment in str(request.url):
                if isinstance(payload, int):
                    return httpx.Response(payload)
                if isinstance(payload, str):
                    return httpx.Response(200, text=payload)
                return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def client(routes: dict[str, object], tmp_path: Path) -> EdgarClient:
    return EdgarClient(
        "filing-answers tests@ememndon.com",
        cache_dir=tmp_path,
        client=fake_transport(routes),
    )


class TestContactAddress:
    def test_refuses_a_user_agent_with_no_address(self, tmp_path: Path) -> None:
        # EDGAR rejects these, so building the client is the right place
        # to find out rather than the first request
        with pytest.raises(ValueError, match="contact email"):
            EdgarClient("filing-answers", cache_dir=tmp_path)

    def test_sends_the_contact_on_every_request(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("user-agent", ""))
            return httpx.Response(200, json=TICKER_INDEX)

        edgar = EdgarClient(
            "filing-answers tests@ememndon.com",
            cache_dir=tmp_path,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        edgar.cik_for("AAPL")
        assert seen and "tests@ememndon.com" in seen[0]


class TestTickerLookup:
    def test_finds_a_company(self, tmp_path: Path) -> None:
        edgar = client({"company_tickers": TICKER_INDEX}, tmp_path)
        cik, name = edgar.cik_for("AAPL")
        # the submissions API needs ten digits, zero padded
        assert cik == "0000320193"
        assert name == "Apple Inc."

    def test_is_case_insensitive_and_forgiving_of_spaces(self, tmp_path: Path) -> None:
        edgar = client({"company_tickers": TICKER_INDEX}, tmp_path)
        assert edgar.cik_for("  aapl ")[0] == "0000320193"

    def test_says_so_when_a_ticker_is_not_registered(self, tmp_path: Path) -> None:
        edgar = client({"company_tickers": TICKER_INDEX}, tmp_path)
        with pytest.raises(UnknownTickerError, match="NOTREAL"):
            edgar.cik_for("NOTREAL")

    def test_fetches_the_index_once_however_many_lookups(self, tmp_path: Path) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=TICKER_INDEX)

        edgar = EdgarClient(
            "filing-answers tests@ememndon.com",
            cache_dir=tmp_path,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        edgar.cik_for("AAPL")
        edgar.cik_for("MSFT")
        edgar.cik_for("AAPL")
        assert calls == 1


class TestFindingTheAnnualReport:
    def test_takes_the_most_recent_annual_report(self, tmp_path: Path) -> None:
        edgar = client({"company_tickers": TICKER_INDEX, "submissions": SUBMISSIONS}, tmp_path)
        filing = edgar.latest_annual_report("AAPL")
        assert filing.form == "10-K"
        assert filing.filed == date(2024, 11, 1)
        # not the 2023 one further down the list
        assert filing.period == date(2024, 9, 28)

    def test_ignores_quarterly_and_event_filings(self, tmp_path: Path) -> None:
        edgar = client({"company_tickers": TICKER_INDEX, "submissions": SUBMISSIONS}, tmp_path)
        assert edgar.latest_annual_report("AAPL").form not in {"8-K", "10-Q"}

    def test_says_so_when_there_is_no_annual_report(self, tmp_path: Path) -> None:
        only_quarterlies = {
            "filings": {
                "recent": {
                    "form": ["10-Q"],
                    "filingDate": ["2024-11-01"],
                    "reportDate": ["2024-09-28"],
                    "accessionNumber": ["0000320193-24-000122"],
                    "primaryDocument": ["q.htm"],
                }
            }
        }
        edgar = client({"company_tickers": TICKER_INDEX, "submissions": only_quarterlies}, tmp_path)
        with pytest.raises(FilingNotFoundError, match="annual report"):
            edgar.latest_annual_report("AAPL")


class TestFilingIdentity:
    def test_builds_the_archive_url_without_the_zero_padding(self) -> None:
        # the submissions API pads the identifier and the archive does not,
        # which is a quiet way to get a 404
        filing = Filing(
            cik="0000320193",
            ticker="AAPL",
            company="Apple Inc.",
            form="10-K",
            filed=date(2024, 11, 1),
            period=date(2024, 9, 28),
            accession="000032019324000123",
            document="aapl-20240928.htm",
        )
        assert filing.url == (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"
        )

    def test_labels_a_filing_by_the_year_it_covers(self) -> None:
        # a report filed in November 2024 covering the year to September
        # 2024 is the 2024 report, and a reader means the period
        filing = Filing(
            cik="0000320193",
            ticker="AAPL",
            company="Apple Inc.",
            form="10-K",
            filed=date(2024, 11, 1),
            period=date(2024, 9, 28),
            accession="x",
            document="y.htm",
        )
        assert filing.label == "AAPL 10-K FY2024"


class TestCaching:
    def test_fetches_once_and_then_reads_from_disk(self, tmp_path: Path) -> None:
        calls = 0
        body = "<html>" + ("annual report " * 2000) + "</html>"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if "Archives" in str(request.url):
                calls += 1
                return httpx.Response(200, text=body)
            if "submissions" in str(request.url):
                return httpx.Response(200, json=SUBMISSIONS)
            return httpx.Response(200, json=TICKER_INDEX)

        edgar = EdgarClient(
            "filing-answers tests@ememndon.com",
            cache_dir=tmp_path,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        filing = edgar.latest_annual_report("AAPL")
        first = edgar.document_html(filing)
        second = edgar.document_html(filing)

        assert first == second
        # a 10-K is megabytes; fetching it twice is rude at someone else's cost
        assert calls == 1


class TestErrorPagesThatLookLikeSuccess:
    def test_rejects_the_courteous_not_found_page(self) -> None:
        # EDGAR answers some bad requests with a polite HTML page and a
        # 200, so a fetch can succeed and return an apology
        assert not looks_like_annual_report(
            "<html><body>The page you requested could not be found.</body></html>"
        )

    def test_rejects_something_far_too_short_to_be_a_filing(self) -> None:
        assert not looks_like_annual_report("<html>annual report</html>")

    def test_accepts_a_real_filing(self) -> None:
        assert looks_like_annual_report("<html>" + ("Annual Report " * 2000) + "</html>")


@pytest.mark.live
def test_against_the_real_sec(tmp_path: Path) -> None:
    """Fetches a real filing. Run with: pytest -m live"""
    with EdgarClient("filing-answers projects@ememndon.com", cache_dir=tmp_path) as edgar:
        filing = edgar.latest_annual_report("AAPL")
        assert filing.form in {"10-K", "10-K405", "10-KSB", "10-KT"}
        html = edgar.document_html(filing)
        assert looks_like_annual_report(html)
