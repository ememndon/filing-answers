"""Configuration refuses to start when it is wrong.

Every case here is a deployment that would otherwise come up looking
healthy and fail later — on the first EDGAR request, or on the first
question a user asks. Failing on the way up turns a production incident
into a container that will not start, which is the trade worth making.
"""

import pytest
from pydantic import ValidationError

from filing_answers.config import Settings


def build(**overrides: str) -> Settings:
    """Settings from an explicit environment, ignoring any local .env."""
    values = {
        "anthropic_api_key": "sk-test-not-a-real-key",
        "sec_user_agent": "filing-answers someone@ememndon.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class TestApiKey:
    def test_accepts_a_key(self) -> None:
        assert build().anthropic_api_key == "sk-test-not-a-real-key"

    def test_refuses_an_empty_key(self) -> None:
        # the .env.example placeholder, copied but never filled in
        with pytest.raises(ValidationError, match="empty"):
            build(anthropic_api_key="   ")

    def test_trims_surrounding_whitespace(self) -> None:
        # a key pasted from a terminal usually arrives with a newline
        assert build(anthropic_api_key="  sk-test-x  \n").anthropic_api_key == "sk-test-x"


class TestSecContact:
    def test_accepts_a_real_contact(self) -> None:
        assert "@" in build().sec_user_agent

    def test_refuses_a_contact_with_no_address(self) -> None:
        # the SEC refuses these requests, so starting up would be a lie
        with pytest.raises(ValidationError, match="contact email"):
            build(sec_user_agent="filing-answers")

    def test_refuses_the_example_address(self) -> None:
        # worse than a missing address: it works, until the regulator
        # tries to reach whoever is making the requests
        with pytest.raises(ValidationError, match="real one"):
            build(sec_user_agent="filing-answers your-email@example.com")

    def test_strips_the_quotes_a_dotenv_file_keeps(self) -> None:
        assert build(sec_user_agent='"filing-answers me@ememndon.com"').sec_user_agent == (
            "filing-answers me@ememndon.com"
        )


class TestDefaults:
    def test_defaults_to_a_small_model(self) -> None:
        # the project's claim is about the check on the answer, not the
        # size of the model, and the default should say so
        assert "haiku" in build().answer_model

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            build(request_timeout_seconds=0)  # type: ignore[arg-type]
