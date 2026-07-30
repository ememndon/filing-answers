"""One question in, one checked answer out.

Everything up to here is a part: fetch a filing, cut it into passages,
rank them, ask a model, check what it said. This is where those become a
thing you can use, and it exists so the web service and the release gate
run the *same* path. A gate that exercises a different route to the
answer than the service does is measuring the wrong program.

The one decision worth arguing about is what happens when verification
fails. The unverified text is not returned. It is thrown away and the
caller is told the answer could not be supported.

That is deliberate and it is the whole point. An answer that fails the
check is not a rough draft or a lower-confidence result, it is a
statement about a company's finances that its own source does not
support, and handing it over with a flag attached invites exactly the
thing the project is meant to prevent: someone reading the sentence,
finding it plausible, and never looking at the flag.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .answer import Answer, ModelCall, ask
from .edgar import EdgarClient, Filing
from .extract import Passage, passages
from .retrieve import Index
from .verify import Verdict, checked

#: How many passages the model is shown. Enough that the answer is
#: usually among them, few enough that it cannot quietly answer from the
#: wrong part of a 300-page document — every extra passage is another
#: place a plausible wrong number can be found.
#:
#: Eight because it was measured, not guessed. Across the forty-two
#: answerable questions in the evaluation set, the passage holding the
#: answer is in the top five for thirty-eight of them and in the top
#: eight for forty-one. Twelve finds nothing that eight does not, so the
#: extra context would be paid for and never used.
#:
#: The remaining one is not a ranking problem. Asked which firm audits
#: BlackRock, the answer is a signature — "/s/ Deloitte & Touche LLP" —
#: sitting in a passage that shares almost no words with the question,
#: while the auditor's report itself matches every word of it and never
#: names anybody. No value of this number reaches it.
PASSAGES_PER_QUESTION = 8

WITHHELD = "The answer could not be verified against the filing, so it has been withheld."


class Result(BaseModel):
    """What a caller receives. Nothing unverified reaches this."""

    answer: str
    citation: str = Field(default="", description="The sentence in the filing the answer rests on")
    source: str = Field(
        default="",
        description='Where in the document it came from, e.g. "AAPL 10-K FY2025, Item 7"',
    )
    verified: bool
    rejected_because: list[str] = Field(
        default_factory=list,
        description="Why an answer was withheld, when one was",
    )

    #: The text that failed the check. Kept for the evaluation and the
    #: logs, excluded from anything a caller is sent — the moment a
    #: rejected answer is transmitted, somebody reads it.
    withheld_text: str = Field(default="", exclude=True)


@dataclass
class Trace:
    """What happened on the way to an answer, for the gate to record."""

    ticker: str
    question: str
    filing: Filing | None = None
    considered: list[Passage] = field(default_factory=list)
    raw: Answer | None = None
    verdict: Verdict | None = None
    seconds: float = 0.0


class FilingIndex:
    """One filing, parsed and searchable.

    Parsing is the expensive step by a wide margin — BlackRock's 10-K is
    twelve megabytes of Inline XBRL — so it happens once per filing and
    the result is held. A run of forty-eight evaluation questions across
    six companies parses six documents, not forty-eight.
    """

    def __init__(self, filing: Filing, html: str) -> None:
        self.filing = filing
        self.passages: list[Passage] = passages(html)
        self.index = Index(self.passages)

    def search(self, question: str, limit: int = PASSAGES_PER_QUESTION):
        return self.index.search(question, limit=limit)


class AnswerService:
    """Answers questions about a company's latest annual report.

    The EDGAR client and the model call are both injected. That is not
    ceremony: it is what lets the release gate run this exact code
    against a deliberately worse model, and what lets the tests run it
    against no model at all.
    """

    def __init__(
        self,
        edgar: EdgarClient,
        call: ModelCall,
        passages_per_question: int = PASSAGES_PER_QUESTION,
    ) -> None:
        self._edgar = edgar
        self._call = call
        # Adjustable so the gate can be run against a deliberately worse
        # configuration and shown to block it. Turning this down is the
        # cheapest way to make the whole system worse — fewer passages is
        # a smaller prompt and a smaller bill — and it is exactly the kind
        # of change that passes every unit test on the way in.
        self._passages = passages_per_question
        self._indexes: dict[str, FilingIndex] = {}

    def index_for(self, ticker: str) -> FilingIndex:
        """The searchable form of a company's latest annual report."""
        key = ticker.strip().upper()
        if key not in self._indexes:
            filing = self._edgar.latest_annual_report(key)
            self._indexes[key] = FilingIndex(filing, self._edgar.document_html(filing))
        return self._indexes[key]

    def ask(self, ticker: str, question: str) -> tuple[Result, Trace]:
        """Answer a question, and say what was done to get there.

        The trace is returned rather than logged so the caller decides
        what to do with it. The service does not know whether it is
        serving a request or being measured.
        """
        started = time.monotonic()
        trace = Trace(ticker=ticker.strip().upper(), question=question)

        index = self.index_for(ticker)
        trace.filing = index.filing

        scored = index.search(question, limit=self._passages)
        trace.considered = [s.passage for s in scored]

        raw = ask(question, scored, self._call)
        trace.raw = raw

        answer, verdict = checked(raw, trace.considered, question)
        trace.verdict = verdict
        trace.seconds = round(time.monotonic() - started, 3)

        return _result(answer, verdict, index.filing), trace


def _result(answer: Answer, verdict: Verdict, filing: Filing) -> Result:
    """Turn a checked answer into what a caller is allowed to see."""
    # Declining to answer is a real answer and passes the check, but it
    # has no citation, so it is reported as itself rather than dressed up
    # with an empty source.
    if not answer.answered:
        return Result(answer=answer.text, verified=True)

    if not verdict.verified:
        return Result(
            answer=WITHHELD,
            verified=False,
            rejected_because=verdict.reasons,
            withheld_text=answer.text,
        )

    where = f"{filing.label}, {answer.section}" if answer.section else filing.label
    return Result(
        answer=answer.text,
        # The quote, not the passage it sits in. The verifier has already
        # confirmed this sentence appears there word for word, and a
        # reader checking an answer wants the line, not two thousand
        # characters of the surrounding page.
        citation=answer.quote,
        source=where,
        verified=True,
    )
