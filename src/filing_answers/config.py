"""Settings, validated once at startup rather than discovered at runtime.

A service that reads its configuration lazily fails in the worst possible
place: halfway through handling a request, in production, with a stack
trace about a missing key. Reading and validating everything on the way up
means a misconfigured deployment refuses to start, which is a far cheaper
kind of failure.

The SEC contact address is required for the same reason it is required by
the SEC: they refuse requests that do not carry one, so a deployment
without it would start, look healthy, and fail on the first real fetch.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything this service needs from its environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str = Field(
        ...,
        description="Key for the model that reads passages and answers questions.",
    )

    answer_model: str = Field(
        default="claude-haiku-4-5",
        description=(
            "Deliberately a small model. The quality this project promises comes "
            "from the check on the answer, not from the size of what produced it."
        ),
    )

    sec_user_agent: str = Field(
        ...,
        description=(
            "Sent on every EDGAR request. The SEC requires a real contact address "
            "and refuses requests without one."
        ),
    )

    request_timeout_seconds: float = Field(default=30.0, gt=0)

    @field_validator("anthropic_api_key")
    @classmethod
    def key_must_look_like_a_key(cls, value: str) -> str:
        # Catches the commonest misconfiguration by far: the placeholder
        # from .env.example copied across without being filled in.
        if not value.strip():
            raise ValueError(
                "ANTHROPIC_API_KEY is empty — copy .env.example to .env and fill it in"
            )
        return value.strip()

    @field_validator("sec_user_agent")
    @classmethod
    def contact_must_be_reachable(cls, value: str) -> str:
        # The SEC uses this address to reach whoever is making the requests.
        # An address that is obviously fake is worse than a refused request:
        # it works until someone at the regulator tries to get in touch.
        cleaned = value.strip().strip('"')
        if "@" not in cleaned:
            raise ValueError(
                "SEC_USER_AGENT must include a contact email address, "
                'for example: "filing-answers you@example.com"'
            )
        if "example.com" in cleaned:
            raise ValueError("SEC_USER_AGENT still holds the example address — use a real one")
        return cleaned


@lru_cache
def settings() -> Settings:
    """The settings, read once per process."""
    return Settings()  # type: ignore[call-arg]
