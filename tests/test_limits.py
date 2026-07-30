"""What stops a public demo becoming a public expense.

Time is injected throughout. A test that waits an hour to prove an
hourly limit resets is a test nobody runs, and a limit nobody tests is a
limit that turns out to be off.
"""

from __future__ import annotations

import pytest

from filing_answers.limits import Limiter, RefusedError


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestOneCaller:
    def test_allows_up_to_the_limit(self) -> None:
        limiter = Limiter(per_caller=3, clock=Clock())
        for _ in range(3):
            limiter.check("1.2.3.4")

    def test_refuses_the_one_after(self) -> None:
        limiter = Limiter(per_caller=3, clock=Clock())
        for _ in range(3):
            limiter.check("1.2.3.4")
        with pytest.raises(RefusedError, match="3 questions"):
            limiter.check("1.2.3.4")

    def test_does_not_hold_it_against_anybody_else(self) -> None:
        limiter = Limiter(per_caller=2, clock=Clock())
        limiter.check("1.2.3.4")
        limiter.check("1.2.3.4")
        limiter.check("5.6.7.8")

    def test_the_window_slides_rather_than_resetting(self) -> None:
        # a fixed window lets a caller spend one allowance at 10:59 and
        # the next at 11:00, which is twice the intended rate at exactly
        # the wrong moment
        clock = Clock()
        limiter = Limiter(per_caller=2, per_caller_window=3600, clock=clock)
        limiter.check("1.2.3.4")
        clock.advance(3599)
        limiter.check("1.2.3.4")

        with pytest.raises(RefusedError):
            limiter.check("1.2.3.4")

        # only the first question ages out, so one slot opens and not two
        clock.advance(2)
        limiter.check("1.2.3.4")
        with pytest.raises(RefusedError):
            limiter.check("1.2.3.4")

    def test_says_when_to_come_back(self) -> None:
        clock = Clock()
        limiter = Limiter(per_caller=1, per_caller_window=3600, clock=clock)
        limiter.check("1.2.3.4")
        clock.advance(600)
        with pytest.raises(RefusedError) as refused:
            limiter.check("1.2.3.4")
        assert 2990 < refused.value.retry_after < 3010


class TestTheDailyCeiling:
    def test_binds_however_many_callers_there_are(self) -> None:
        # the limit that actually protects the bill. A per-caller limit
        # is defeated by using more callers; only a global one can
        # promise that tomorrow's invoice is bounded.
        limiter = Limiter(per_caller=100, per_day=5, clock=Clock())
        for n in range(5):
            limiter.check(f"10.0.0.{n}")
        with pytest.raises(RefusedError, match="limited number of questions a day"):
            limiter.check("10.0.0.99")

    def test_is_checked_before_the_per_caller_limit(self) -> None:
        # so a caller who is within their own allowance is told the true
        # reason rather than a misleading one
        limiter = Limiter(per_caller=100, per_day=1, clock=Clock())
        limiter.check("1.2.3.4")
        with pytest.raises(RefusedError, match="a day"):
            limiter.check("5.6.7.8")

    def test_recovers_the_next_day(self) -> None:
        clock = Clock()
        limiter = Limiter(per_day=2, clock=clock)
        limiter.check("1.2.3.4")
        limiter.check("1.2.3.4")
        with pytest.raises(RefusedError):
            limiter.check("1.2.3.4")
        clock.advance(86_401)
        limiter.check("1.2.3.4")

    def test_reports_what_has_been_spent(self) -> None:
        limiter = Limiter(clock=Clock())
        for n in range(4):
            limiter.check(f"10.0.0.{n}")
        assert limiter.asked_today == 4


class TestARefusedQuestionCostsNothing:
    def test_a_refusal_is_not_counted_against_the_day(self) -> None:
        # otherwise a caller hammering a closed door would keep the door
        # closed for everybody, long after the real traffic stopped
        clock = Clock()
        limiter = Limiter(per_caller=1, per_day=10, clock=clock)
        limiter.check("1.2.3.4")
        for _ in range(20):
            with pytest.raises(RefusedError):
                limiter.check("1.2.3.4")
        assert limiter.asked_today == 1
