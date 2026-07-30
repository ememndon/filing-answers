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
from filing_answers.gate import COOKIE, Attempts
from filing_answers.limits import Limiter
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
    # Over HTTPS, because the session cookie is set Secure and a client
    # on plain HTTP will correctly refuse to store it. Discovered by a
    # test that logged in successfully and was then locked out — which is
    # the flag working, and is also how the deployment actually runs.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture(autouse=True)
def clean_up():
    yield
    app.dependency_overrides.clear()
    for held in ("service", "limiter", "password", "attempts"):
        if hasattr(app.state, held):
            delattr(app.state, held)


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


class TestTheSpendingLimit:
    """A public URL turns anonymous requests into charges on a card."""

    def setup_client(self, per_day: int = 2):
        service = FakeService(GOOD)
        client = client_for(service)
        app.state.limiter = Limiter(per_caller=100, per_day=per_day)
        return client, service

    def payload(self) -> dict[str, str]:
        return {"ticker": "AAPL", "question": "What were total net sales?"}

    def test_answers_up_to_the_daily_ceiling(self) -> None:
        client, _ = self.setup_client(per_day=2)
        assert client.post("/ask", json=self.payload()).status_code == 200
        assert client.post("/ask", json=self.payload()).status_code == 200

    def test_then_refuses_with_429_rather_than_403(self) -> None:
        # nothing is wrong with the request; there is simply no more
        # budget for it, and the two deserve different codes
        client, _ = self.setup_client(per_day=1)
        client.post("/ask", json=self.payload())
        refused = client.post("/ask", json=self.payload())
        assert refused.status_code == 429
        assert "questions a day" in refused.json()["detail"]

    def test_tells_the_caller_when_to_come_back(self) -> None:
        client, _ = self.setup_client(per_day=1)
        client.post("/ask", json=self.payload())
        refused = client.post("/ask", json=self.payload())
        assert int(refused.headers["Retry-After"]) > 0

    def test_a_refused_question_never_reaches_the_model(self) -> None:
        # the whole point: the request is stopped before it costs anything
        client, service = self.setup_client(per_day=1)
        client.post("/ask", json=self.payload())
        client.post("/ask", json=self.payload())
        assert len(service.asked) == 1


class TestThePasswordGate:
    """Ways in that are not meant to work."""

    def gated(self, password: str = "let-me-in-please"):
        service = FakeService(GOOD)
        client = client_for(service)
        app.state.password = password
        app.state.attempts = Attempts()
        return client, service, password

    def test_the_page_is_not_served_without_it(self) -> None:
        client, _, _ = self.gated()
        response = client.get("/")
        assert response.status_code == 401
        assert 'name="password"' in response.text

    def test_the_api_is_not_either(self) -> None:
        # a gate on the page and an open endpoint is decoration — the
        # expensive part is behind the endpoint
        client, service, _ = self.gated()
        response = client.post("/ask", json={"ticker": "AAPL", "question": "What were sales?"})
        assert response.status_code == 401
        assert service.asked == []

    def test_the_docs_are_not_either(self) -> None:
        client, _, _ = self.gated()
        assert client.get("/docs").status_code == 401

    def test_health_and_ready_answer_regardless(self) -> None:
        # an orchestrator has no browser and no password, and a container
        # that cannot report its own health gets restarted forever
        client, _, _ = self.gated()
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200

    def test_the_right_password_lets_you_in(self) -> None:
        client, _, password = self.gated()
        entered = client.post("/enter", data={"password": password}, follow_redirects=False)
        assert entered.status_code == 303
        assert client.get("/").status_code == 200

    def test_the_wrong_one_does_not(self) -> None:
        client, _, _ = self.gated()
        refused = client.post("/enter", data={"password": "guess"}, follow_redirects=False)
        assert refused.status_code == 401
        assert client.get("/").status_code == 401

    def test_the_cookie_cannot_simply_be_set(self) -> None:
        # the reason it is signed
        client, _, _ = self.gated()
        client.cookies.set(COOKIE, "yes")
        assert client.get("/").status_code == 401

    def test_the_cookie_is_httponly_and_secure(self) -> None:
        client, _, password = self.gated()
        entered = client.post("/enter", data={"password": password}, follow_redirects=False)
        cookie = entered.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=lax" in cookie

    def test_guessing_is_stopped_before_it_gets_far(self) -> None:
        client, _, _ = self.gated()
        for _ in range(8):
            client.post("/enter", data={"password": "wrong"}, follow_redirects=False)
        blocked = client.post("/enter", data={"password": "wrong"}, follow_redirects=False)
        assert blocked.status_code == 429

    def test_the_lockout_holds_even_for_the_right_password(self) -> None:
        # otherwise the limit is a speed bump a script drives over
        client, _, password = self.gated()
        for _ in range(8):
            client.post("/enter", data={"password": "wrong"}, follow_redirects=False)
        assert client.post("/enter", data={"password": password}).status_code == 429

    def test_no_gate_at_all_when_no_password_is_configured(self) -> None:
        # a fresh clone gets a working service, not a locked door
        service = FakeService(GOOD)
        client = client_for(service)
        app.state.password = ""
        assert client.get("/").status_code == 200
        assert (
            client.post("/ask", json={"ticker": "AAPL", "question": "What were sales?"}).status_code
            == 200
        )
