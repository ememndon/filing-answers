"""The service.

Three endpoints and no more. One answers questions; the other two exist
so something running this in a container can tell the difference between
"the process is alive" and "the process can do its job", which are not
the same fact and should never be served by the same handler.

The interesting decisions here are about what does not happen.

Nothing unverified is returned. An answer that failed the check comes
back as a refusal with a reason, and the text that failed is not in the
response at all — not in a field, not behind a flag. That is enforced one
layer down, in the pipeline, so this cannot get it wrong by forgetting.

The filings are parsed once and held. A 10-K is megabytes of Inline XBRL
and takes a second or two to cut into passages; doing that per request
would make the first question of every minute slow for no reason. The
cache is in memory and per process, which is the right size for it —
persisting parsed filings would mean invalidating them, and a stale
passage is a wrong citation.

Startup is where configuration is checked, and a misconfigured deployment
refuses to start rather than failing on its first real request.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from anthropic import APIError
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .answer import anthropic_caller
from .config import Settings, settings
from .edgar import EdgarClient, FilingNotFoundError, UnknownTickerError
from .pipeline import AnswerService, Result

log = structlog.get_logger()


class Question(BaseModel):
    """What a caller sends."""

    ticker: str = Field(
        min_length=1,
        max_length=10,
        description="The company's ticker, as registered with the SEC",
    )
    question: str = Field(
        min_length=5,
        max_length=500,
        description="A question about the company's most recent annual report",
    )


class Health(BaseModel):
    status: str
    filings_cached: int = Field(description="Annual reports parsed and held in this process")


def build_service(config: Settings) -> AnswerService:
    edgar = EdgarClient(config.sec_user_agent, timeout=config.request_timeout_seconds)
    call = anthropic_caller(
        config.anthropic_api_key, config.answer_model, config.request_timeout_seconds
    )
    return AnswerService(edgar, call)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Reading the settings here is what makes a bad deployment fail on the
    # way up. Doing it lazily would mean a container that starts, reports
    # healthy, and returns a stack trace to the first person who uses it.
    config = settings()
    app.state.service = build_service(config)
    log.info("started", model=config.answer_model, threshold=config.threshold)
    yield
    app.state.service.close()


app = FastAPI(
    title="filing-answers",
    description="Answers questions about annual reports, and withholds answers it cannot support.",
    version="0.1.0",
    lifespan=lifespan,
)


def current_service(request: Request) -> AnswerService:
    """The service this process started with.

    Taken off application state rather than built per request, because
    the parsed filings live on it and rebuilding would throw them away.
    Reached through the request so that a test can substitute its own.
    """
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="still starting")
    return service


def held(request: Request) -> int:
    service = getattr(request.app.state, "service", None)
    return service.filings_held if service else 0


@app.get("/health", response_model=Health, summary="Is the process alive")
async def health(request: Request) -> Health:
    """Liveness. Answers as long as the process is running.

    Deliberately touches nothing. A liveness check that calls out to
    another service will one day report this one as dead because that one
    is, and something will restart a perfectly healthy container.
    """
    return Health(status="alive", filings_cached=held(request))


@app.get("/ready", response_model=Health, summary="Can the process do its job")
async def ready(request: Request) -> Health:
    """Readiness. Answers only once configuration has been read and accepted.

    Separate from liveness because the two have different consequences:
    a process that is alive but not ready should stop receiving traffic,
    not be killed and restarted.
    """
    if getattr(request.app.state, "service", None) is None:
        raise HTTPException(status_code=503, detail="still starting")
    return Health(status="ready", filings_cached=held(request))


@app.post("/ask", response_model=Result, summary="Ask a question about a filing")
async def ask(
    request: Question,
    service: Annotated[AnswerService, Depends(current_service)],
) -> Result:
    """Answer a question from the company's most recent annual report.

    A 200 here does not mean the answer is good, it means the request was
    understood. Whether the answer may be relied on is the `verified`
    field, and when it is false the answer itself has already been
    withheld and `rejected_because` says why.

    That is on purpose. Returning an error status for an unsupported
    answer would put the refusal in the same bucket as a typo in the
    ticker, and a caller retrying on 5xx would retry a question that is
    going to be refused again.
    """
    try:
        result, trace = service.ask(request.ticker, request.question)
    except UnknownTickerError as unknown:
        raise HTTPException(status_code=404, detail=str(unknown)) from unknown
    except FilingNotFoundError as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from missing
    except APIError as upstream:
        # The message is logged and not returned. An upstream error can
        # carry request details, and a caller does not need them.
        log.error("model call failed", error=str(upstream))
        raise HTTPException(status_code=502, detail="the model could not be reached") from upstream

    log.info(
        "answered",
        ticker=request.ticker,
        verified=result.verified,
        seconds=trace.seconds,
        section=result.source,
    )
    return result
