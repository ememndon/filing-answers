"""Deciding whether an answer is supported by the passage it cited.

This is the module the project is named for. Everything else is plumbing
that could be swapped for something else; this is the promise being made.

Two checks, in order.

The quote has to be real. A model that cannot produce the sentence it
used did not use one, and an answer built on a sentence nobody wrote is
not an answer, however plausible it sounds.

Every figure in the answer has to be in that sentence. This is the one
that catches the failure worth caring about: a model asked for a number
it cannot find will supply one, or will helpfully round a real one into
something the filing never said. "$391 million" where the source says
"391,035" is not a smaller version of the truth, it is a different
number, and a firm acting on it is acting on nothing.

Both checks are mechanical. They cannot be talked round by a confident
tone, which is exactly why they are here and not in a prompt.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field

from .answer import Answer
from .extract import Passage

#: Anything a reader would call a figure: 391,035 · 46.2 · 2025 · 1.5
FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: A quote shorter than this identifies nothing. Half a dozen words appear
#: all over a filing, so matching one proves the model can echo common
#: language rather than that it read the passage it named.
MIN_QUOTE_CHARS = 25


class Verdict(BaseModel):
    """Whether an answer may be shown, and what was wrong if not."""

    verified: bool
    quote_found: bool = False
    unsupported_figures: list[str] = Field(
        default_factory=list,
        description="Figures stated in the answer that its own source does not contain",
    )
    reasons: list[str] = Field(default_factory=list)
    source: Passage | None = Field(
        default=None,
        description="The passage the quote was actually found in, which is what gets cited",
    )
    misattributed: bool = Field(
        default=False,
        description="The model quoted accurately but named a different passage",
    )

    @property
    def summary(self) -> str:
        return "supported by the citation" if self.verified else "; ".join(self.reasons)


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def figures_in(text: str) -> list[str]:
    """Every figure in a piece of text, as written."""
    return FIGURE.findall(text)


def _value(figure: str) -> Decimal | None:
    """A figure as a number, so 391,035 and 391035 are the same thing.

    Comparing the written form would reject an answer for using a comma
    differently, which is pedantry rather than verification. Comparing
    the value still catches the failure that matters, because a model
    that rounds 391,035 to 391 has produced a different number and not a
    tidier one.
    """
    try:
        return Decimal(figure.replace(",", ""))
    except InvalidOperation:
        return None


def quote_is_real(quote: str, passage: Passage) -> bool:
    """Whether the cited sentence actually appears in the cited passage.

    Whitespace is normalised on both sides. A model copying a line
    accurately still returns it with the line breaks rearranged, and
    rejecting an answer over that would fail honest work while catching
    no dishonest work at all.
    """
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        return False
    return _normalise(quote) in _normalise(passage.text)


def unsupported_figures(answer_text: str, passage: Passage) -> list[str]:
    """Figures the answer states that its source does not contain."""
    available = {_value(f) for f in figures_in(passage.text)}
    available.discard(None)

    missing: list[str] = []
    for figure in figures_in(answer_text):
        value = _value(figure)
        if value is None or value in available:
            continue
        if figure not in missing:
            missing.append(figure)
    return missing


def verify(answer: Answer, offered: list[Passage]) -> Verdict:
    """Decide whether an answer may be shown to anyone.

    Checked against every passage the model was OFFERED, not only the one
    it named. The quote is the evidence and the index is bookkeeping, and
    a model can get the second wrong while getting the first exactly
    right — against Apple's filing it quoted the revenue line word for
    word and then labelled it passage 77 when the sentence was in 96.
    Failing that as a fabrication would reject a correct, grounded answer
    for a clerical error.

    This does not loosen the check. The quote still has to appear, word
    for word, in something the model was actually shown; a sentence found
    nowhere in that set is invented no matter how well it reads. What it
    fixes is which passage the figures are then checked against, and
    which one gets cited to the reader — the one that really contains the
    sentence rather than the one the model pointed at.

    An answer that declines to answer passes: saying the filing does not
    contain something is a correct and useful reply, and there is nothing
    in it to be unsupported.
    """
    if not answer.answered:
        return Verdict(verified=True, quote_found=True)

    if not offered:
        return Verdict(verified=False, reasons=["no passage was provided to answer from"])

    source = next((p for p in offered if quote_is_real(answer.quote, p)), None)
    if source is None:
        return Verdict(
            verified=False,
            reasons=[
                "the quoted sentence appears in none of the passages provided"
                if answer.quote.strip()
                else "no sentence was quoted"
            ],
        )

    reasons: list[str] = []
    missing = unsupported_figures(answer.text, source)
    if missing:
        reasons.append(
            f"the citation does not contain {', '.join(missing)}"
            if len(missing) > 1
            else f"the citation does not contain {missing[0]}"
        )

    return Verdict(
        verified=not reasons,
        quote_found=True,
        unsupported_figures=missing,
        reasons=reasons,
        source=source,
        misattributed=source.index != answer.passage_index,
    )


def checked(answer: Answer, offered: list[Passage]) -> tuple[Answer, Verdict]:
    """An answer with its verdict recorded, and its citation corrected.

    Returned together rather than folded into one object so a caller has
    to look at the verdict. An answer that quietly carried a boolean would
    invite being rendered without anybody reading it.
    """
    verdict = verify(answer, offered)
    answer.verified = verdict.verified
    # Point the answer at the passage that really holds its quote, so what
    # a reader is shown is where the sentence actually is.
    if verdict.source is not None:
        answer.passage_index = verdict.source.index
        answer.section = verdict.source.section
    return answer, verdict
