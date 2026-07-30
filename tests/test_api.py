"""The service, without a model, a network or a filing behind it.

The dependency is overridden rather than mocked at the boundary, which
means these tests exercise the real routing, the real validation and the
real response shape, and stop exactly where the interesting behaviour
stops being the web layer's.

The test that matters is the one asserting a withheld answer's text is
absent from the response body. Everything else here is plumbing; that
line is the promise.
"""

from __future__ import annotations

from typing import Any

import pytest
from anthropic import APIError
from fastapi.testclient import TestClient

from filing_answers.api import app, current_service
from filing_answers.edgar import Filing, UnknownTickerError
from filing_answers.pipeline import WITHHELD, Result, Trace

FILING = Filing(
    cik="0000320193",
    ticker="AAPL",
    company="Apple Inc.",
    form="10-K",
    filed="2025-10-31",
    period="2025-09-27",
    accession="000032019325000079",
    document="aapl-20250927.htm",
)


class FakeService:
    """Answers with whatever it was told to answer with."""

    def __init__(self, result: Result | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.asked: list[tuple[str, str]] = []
        self.closed = False

    @property
    def filings_held(self) -> int:
        return 2

    def close(self) -> None:
        self.closed = True

    def ask(self, ticker: str, question: str) -> tuple[Result, Trace]:
        self.asked.append((ticker, question))
        if self._raises:
            raise self._raises
        assert self._result is not None
        return self._result, Trace(ticker=ticker, question=question, filing=FILING, seconds=0.4)


def client_for(service: Any) -> TestClient:
    app.dependency_overrides[current_service] = lambda: service
    # The state is what /health and /ready read, and they do not go
    # through the dependency.
    app.state.service = service
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_up():
    yield
    app.dependency_overrides.clear()
    if hasattr(app.state, "service"):
        del app.state.service


GOOD = Result(
    answer="Total net sales were 416,161 million in fiscal 2025.",
    citation="Total net sales  416,161  391,035  383,285",
    source="AAPL 10-K FY2025, Item 8",
    verified=True,
)


class TestAskingAQuestion:
    def test_returns_the_answer_with_its_citation(self) -> None:
        response = client_for(FakeService(GOOD)).post(
            "/ask", json={"ticker": "AAPL", "question": "What were total net sales?"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == GOOD.answer
        assert "416,161" in body["citation"]
        assert body["source"] == "AAPL 10-K FY2025, Item 8"
        assert body["verified"] is True

    def test_passes_the_question_through_unchanged(self) -> None:
        service = FakeService(GOOD)
        client_for(service).post("/ask", json={"ticker": "BLK", "question": "What was revenue?"})
        assert service.asked == [("BLK", "What was revenue?")]


class TestAWithheldAnswer:
    WITHHELD_RESULT = Result(
        answer=WITHHELD,
        verified=False,
        rejected_because=["the citation does not contain 500,000"],
        withheld_text="Total net sales were 500,000 million.",
    )

    def test_comes_back_as_a_refusal_not_an_error(self) -> None:
        # a refused answer is a understood request, not a broken one, and
        # a caller retrying on 5xx would retry something certain to be
        # refused again
        response = client_for(FakeService(self.WITHHELD_RESULT)).post(
            "/ask", json={"ticker": "AAPL", "question": "What were total net sales?"}
        )
        assert response.status_code == 200
        assert response.json()["verified"] is False

    def test_never_puts_the_unsupported_text_on_the_wire(self) -> None:
        # the line this project exists to make true
        response = client_for(FakeService(self.WITHHELD_RESULT)).post(
            "/ask", json={"ticker": "AAPL", "question": "What were total net sales?"}
        )
        assert "500,000 million" not in response.text
        assert response.json()["answer"] == WITHHELD

    def test_says_why(self) -> None:
        response = client_for(FakeService(self.WITHHELD_RESULT)).post(
            "/ask", json={"ticker": "AAPL", "question": "What were total net sales?"}
        )
        assert response.json()["rejected_because"] == ["the citation does not contain 500,000"]


class TestWhenSomethingIsWrong:
    def test_an_unknown_ticker_is_a_404(self) -> None:
        service = FakeService(raises=UnknownTickerError("'NOPE' is not a ticker"))
        response = client_for(service).post(
            "/ask", json={"ticker": "NOPE", "question": "What were total net sales?"}
        )
        assert response.status_code == 404
        assert "not a ticker" in response.json()["detail"]

    def test_an_upstream_failure_is_a_502_that_says_nothing_useful_to_an_attacker(self) -> None:
        failure = APIError(
            "upstream said something with a request id in it", request=None, body=None
        )  # type: ignore[arg-type]
        response = client_for(FakeService(raises=failure)).post(
            "/ask", json={"ticker": "AAPL", "question": "What were total net sales?"}
        )
        assert response.status_code == 502
        assert response.json()["detail"] == "the model could not be reached"
        assert "request id" not in response.text

    def test_an_empty_question_is_rejected_before_anything_is_spent(self) -> None:
        service = FakeService(GOOD)
        response = client_for(service).post("/ask", json={"ticker": "AAPL", "question": ""})
        assert response.status_code == 422
        assert service.asked == []

    def test_an_overlong_question_is_rejected_too(self) -> None:
        service = FakeService(GOOD)
        response = client_for(service).post(
            "/ask", json={"ticker": "AAPL", "question": "why " * 400}
        )
        assert response.status_code == 422
        assert service.asked == []

    def test_a_missing_field_is_rejected(self) -> None:
        assert (
            client_for(FakeService(GOOD)).post("/ask", json={"ticker": "AAPL"}).status_code == 422
        )


class TestHealthAndReadiness:
    def test_alive_answers_without_touching_anything(self) -> None:
        response = client_for(FakeService(GOOD)).get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    def test_ready_reports_what_is_held(self) -> None:
        response = client_for(FakeService(GOOD)).get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "filings_cached": 2}

    def test_ready_refuses_before_the_service_exists(self) -> None:
        # a process that is alive but not ready should stop receiving
        # traffic, not be restarted.
        #
        # The lifespan is deliberately not run here. Starting it would
        # read the real configuration, and a suite whose result depends on
        # whether a .env happens to exist on this machine is a debugging
        # session nobody enjoys.
        if hasattr(app.state, "service"):
            del app.state.service
        assert TestClient(app).get("/ready").status_code == 503
