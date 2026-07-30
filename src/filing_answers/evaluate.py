"""The release gate.

Everything else in this project decides whether one answer is supported.
This decides whether a *version* is fit to ship, by putting forty-eight
questions with known answers to it and refusing the release if too few
come back right.

The distinction matters. A model that gets a question wrong is a normal
Tuesday. A model that got quietly worse and shipped anyway is an
incident, and nothing about a language model tells you which of the two
you have — the wrong answer arrives in the same tone as the right one.
The only way to know is to ask it things you already know the answer to,
every time, and to make the answer to "may this ship" a number rather
than an opinion.

Two numbers are reported and they mean different things.

Accuracy is how often the answer was right. It is the one that moves when
the model changes, and the threshold is set against it.

Unsupported figures is how often a figure reached an answer that its own
citation did not contain. That one is not a quality measure, it is a
safety measure, and its acceptable value is zero. A release that gets
more questions right while inventing one number is worse than the release
before it, not better.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .pipeline import AnswerService, Result
from .verify import figure_value, figures_in


#: Where to look for the questions. Data, not code, so the set can be
#: read and argued with by someone who does not want to read Python —
#: which means it lives beside the source rather than inside it, and has
#: to be found rather than imported.
#:
#: In a checkout that is two directories up from this file. Installed
#: into a container it is not: the package sits in site-packages and the
#: question set sits next to the working directory, so both are tried.
#: The environment variable is for anyone running the gate against a
#: question set of their own, which is the whole point of keeping the
#: expected answers out of the code.
def questions_path() -> Path:
    override = os.environ.get("FILING_ANSWERS_QUESTIONS")
    candidates = [
        Path(override) if override else None,
        Path(__file__).resolve().parents[2] / "evaluation" / "questions.json",
        Path.cwd() / "evaluation" / "questions.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    looked = ", ".join(str(c) for c in candidates if c)
    raise FileNotFoundError(f"no question set found. Looked in: {looked}")


class Question(BaseModel):
    """One question, and what a right answer to it looks like."""

    ticker: str
    question: str

    figures: list[str | list[str]] = Field(
        default_factory=list,
        description=(
            "Figures the answer must state, compared by value rather than by spelling. "
            "A list of alternatives is satisfied by any one of them, for facts the "
            "filing states in more than one form"
        ),
    )
    phrases: list[str] = Field(
        default_factory=list,
        description="Wording the answer must contain, for questions whose answer is not a number",
    )
    declines: bool = Field(
        default=False,
        description="The filing does not answer this, and saying so is the right answer",
    )
    source: str = Field(
        default="",
        description="Where the expected answer was read from, so the key can be audited",
    )


class Outcome(BaseModel):
    """What happened when one question was put to the service."""

    question: Question
    correct: bool
    why: str = Field(default="", description="What was wrong, when something was")
    answer: str = ""
    verified: bool = True
    unsupported: list[str] = Field(default_factory=list)


class Report(BaseModel):
    """The result of a whole run, and whether it may ship."""

    outcomes: list[Outcome]
    threshold: float

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def correct(self) -> int:
        return sum(1 for o in self.outcomes if o.correct)

    @property
    def accuracy(self) -> float:
        return 100.0 * self.correct / self.total if self.total else 0.0

    @property
    def unsupported(self) -> int:
        """Answers that stated a figure their own citation did not contain."""
        return sum(1 for o in self.outcomes if o.unsupported)

    @property
    def withheld(self) -> int:
        return sum(1 for o in self.outcomes if not o.verified)

    @property
    def passed(self) -> bool:
        # Both conditions, and the second is not negotiable by the first.
        # Getting more questions right while inventing a figure is not an
        # improvement, and a threshold that could be met that way would be
        # measuring the wrong thing.
        return self.accuracy >= self.threshold and self.unsupported == 0


def load(path: Path | None = None) -> list[Question]:
    """The question set, from disk."""
    raw = json.loads((path or questions_path()).read_text(encoding="utf-8"))
    return [Question(**q) for q in raw["questions"]]


def states_figure(text: str, wanted: str | list[str]) -> bool:
    """Whether an answer states a figure, however it chose to write it.

    Compared by value, so "416,161" is satisfied by "$416,161 million"
    and by "416161". The check that a figure is *supported* is the
    verifier's job and is far stricter; this only asks whether the answer
    contains the number the filing gives.

    A list of alternatives is satisfied by any one of them, because a
    filing states the same fact in more than one place and not always in
    the same units. BlackRock's operating income is "7,045" in the table
    in Item 7 and "$7.0 billion" in the sentence beside it. Both are the
    filing's own words for the same thing, and a set that accepted only
    the first would be marking the model on which page it read rather
    than on whether it was right.
    """
    options = [wanted] if isinstance(wanted, str) else wanted
    stated = [figure_value(f) for f in figures_in(text)]
    return any(
        target is not None and target in stated
        for target in (figure_value(option) for option in options)
    )


def grade(question: Question, result: Result, declined: bool) -> tuple[bool, str]:
    """Whether one answer was right, and what was wrong with it if not.

    Pure, so the marking can be tested without a model, a network or a
    filing. A gate whose own arithmetic is untested is not a gate.
    """
    if question.declines:
        if declined:
            return True, ""
        return False, "answered a question the filing does not answer"

    if declined:
        return False, "declined a question the filing does answer"

    if not result.verified:
        return False, f"withheld — {'; '.join(result.rejected_because)}"

    missing = [f for f in question.figures if not states_figure(result.answer, f)]
    if missing:
        wanted = [f if isinstance(f, str) else " or ".join(f) for f in missing]
        return False, f"does not state {', '.join(wanted)}"

    unsaid = [p for p in question.phrases if p.lower() not in result.answer.lower()]
    if unsaid:
        return False, f"does not mention {', '.join(unsaid)}"

    return True, ""


def run(questions: list[Question], service: AnswerService, threshold: float) -> Report:
    """Put every question to the service and mark the answers.

    The service is passed in rather than built here, which is what lets
    the gate be run against a different model without touching it — the
    point being that the thing measured is the thing served, and the only
    difference between a passing run and a failing one is what is behind
    the seam.
    """
    outcomes: list[Outcome] = []
    for question in questions:
        result, trace = service.ask(question.ticker, question.question)
        declined = trace.raw is not None and not trace.raw.answered
        correct, why = grade(question, result, declined)
        outcomes.append(
            Outcome(
                question=question,
                correct=correct,
                why=why,
                answer=result.answer if result.verified else result.withheld_text,
                verified=result.verified,
                unsupported=trace.verdict.unsupported_figures if trace.verdict else [],
            )
        )
    return Report(outcomes=outcomes, threshold=threshold)


def render(report: Report, *, show_failures: bool = True) -> str:
    """The report, as something a person reads in a terminal."""
    lines = [
        "",
        f"  questions           {report.total:>4}",
        f"  answered correctly  {report.correct:>4}  ({report.accuracy:.1f}%)",
        f"  unsupported figures {report.unsupported:>4}",
        f"  withheld            {report.withheld:>4}",
        f"  threshold           {report.threshold:>7.1f}%",
        "",
    ]

    failures = [o for o in report.outcomes if not o.correct]
    if failures and show_failures:
        lines.append("  what went wrong")
        for outcome in failures:
            lines.append(f"    {outcome.question.ticker}  {outcome.question.question}")
            lines.append(f"        {outcome.why}")
        lines.append("")

    lines.append("  PASS" if report.passed else "  FAIL — release blocked")
    lines.append("")
    return "\n".join(lines)
