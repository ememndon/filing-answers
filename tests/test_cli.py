"""The exit codes, which are the only part of this a machine reads.

A person runs the gate and reads the table. Continuous integration runs
it and reads one integer, so that integer has to carry the distinction
between the things that can go wrong — and the distinction that matters
is between "the answers got worse" and "nothing was measured at all".

Those two want opposite responses. One should stop a release; the other
should send somebody to add a secret. The first version of this returned
1 for both, which is how a red tick on this repository came to mean
"you forgot a key" while looking exactly like "the model degraded".
"""

from __future__ import annotations

import pytest

from filing_answers.__main__ import main
from filing_answers.config import settings


@pytest.fixture(autouse=True)
def forget_settings():
    # Read once per process in normal use, which is right there and wrong
    # here: each test needs its own environment to be the one that counts.
    settings.cache_clear()
    yield
    settings.cache_clear()


class TestWhenItCannotRunAtAll:
    def test_a_missing_key_exits_three_rather_than_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SEC_USER_AGENT", "filing-answers someone@example.org")
        assert main(["evaluate"]) == 3

    def test_a_missing_contact_address_does_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # the SEC refuses requests without one, so a deployment lacking it
        # would start, look healthy, and fail on its first real fetch
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
        monkeypatch.setenv("SEC_USER_AGENT", "filing-answers")
        assert main(["evaluate"]) == 3

    def test_it_says_which_setting_is_wrong_without_a_stack_trace(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SEC_USER_AGENT", "")
        main(["evaluate"])
        printed = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in printed
        assert "SEC_USER_AGENT" in printed
        assert "Traceback" not in printed

    def test_it_says_plainly_that_nothing_was_measured(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # so that neither a person nor a CI summary reads the failure as
        # a verdict on the answers
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SEC_USER_AGENT", "")
        main(["evaluate"])
        assert "not a failing evaluation" in capsys.readouterr().out

    def test_the_same_holds_for_a_single_question(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("SEC_USER_AGENT", "")
        assert main(["ask", "AAPL", "What were total net sales?"]) == 3
