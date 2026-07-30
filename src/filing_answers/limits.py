"""Limits, because every question costs money.

A prototype has no spending limit. It runs on one person's laptop, it is
asked a dozen questions a day, and the bill is rounding error. The moment
the same code has a public address, that stops being true: it is now an
endpoint that converts anonymous HTTP requests into charges on somebody's
card, and it will be found. Not maliciously, necessarily — a crawler
following links is enough.

This is the smallest honest version of a control that every production
LLM service needs and almost no prototype has. Two limits, because they
protect against different things:

    per caller   one person, or one script, cannot monopolise it
    per day      the total bill has a ceiling, whoever is asking

The second is the one that matters. A per-caller limit is trivially
defeated by using more callers; only a global ceiling can promise that
tomorrow's invoice is bounded.

Held in memory, which is the right size for this. A shared counter in
Redis would survive a restart and coordinate across replicas, and this
has one process and a bill measured in pennies — the day it has neither
is the day to reach for that.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RefusedError(Exception):
    """The request was within its rights and still cannot be served."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Limiter:
    """Counts what has been asked, and says when to stop.

    A sliding window rather than a fixed one. Fixed windows let a caller
    spend a whole allowance at 10:59 and the next at 11:00, which is
    twice the intended rate at exactly the wrong moment.

    Time is injected so the tests do not sleep. A test that waits an hour
    to prove an hourly limit resets is a test nobody runs.
    """

    def __init__(
        self,
        per_caller: int = 20,
        per_caller_window: int = 3600,
        per_day: int = 500,
        clock=time.monotonic,
    ) -> None:
        self.per_caller = per_caller
        self.per_caller_window = per_caller_window
        self.per_day = per_day
        self._clock = clock
        self._callers: dict[str, deque[float]] = defaultdict(deque)
        self._day: deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _trim(seen: deque[float], now: float, window: float) -> None:
        while seen and now - seen[0] > window:
            seen.popleft()

    def check(self, caller: str) -> None:
        """Record one question, or raise Refused explaining which limit bit."""
        now = self._clock()
        with self._lock:
            self._trim(self._day, now, 86_400)
            if len(self._day) >= self.per_day:
                raise RefusedError(
                    "This demo answers a limited number of questions a day and has "
                    "reached today's. It runs on one person's API key.",
                    retry_after=int(86_400 - (now - self._day[0])) + 1,
                )

            mine = self._callers[caller]
            self._trim(mine, now, self.per_caller_window)
            if len(mine) >= self.per_caller:
                raise RefusedError(
                    f"You have asked {self.per_caller} questions in the last hour, "
                    "which is this demo's limit per visitor.",
                    retry_after=int(self.per_caller_window - (now - mine[0])) + 1,
                )

            mine.append(now)
            self._day.append(now)

            # Callers who have gone quiet are forgotten, so a long-running
            # process does not accumulate a dictionary entry per address
            # it has ever seen.
            if len(self._callers) > 2_000:
                for address in [k for k, v in self._callers.items() if not v]:
                    del self._callers[address]

    @property
    def asked_today(self) -> int:
        with self._lock:
            self._trim(self._day, self._clock(), 86_400)
            return len(self._day)
