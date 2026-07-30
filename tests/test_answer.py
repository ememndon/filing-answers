"""Putting a question to a model and shaping what comes back.

No model is called here. The interesting behaviour is not what a model
says — that is the verifier's problem — but what this module does around
it: what it sends, what it does when there is nothing to send, and what
it does when the model fails or answers oddly.

The stub below is the whole reason ModelCall is a protocol with one
method. A narrow seam is a testable one.
"""

from __future__ import annotations

from typing import Any

from filing_answers.answer import (
    Answer,
    ask,
    build_message,
    passage_by_index,
)
from filing_answers.extract import Passage
from filing_answers.retrieve import Scored

PASSAGES = [
    Scored(
        passage=Passage(
            text="Total net sales  416,161  391,035  383,285",
            section="Item 7",
            index=12,
        ),
        score=9.0,
    ),
    Scored(
        passage=Passage(
            text="The Company had approximately 166,000 full-time equivalent employees.",
            section="Item 1",
            index=3,
        ),
        score=4.0,
    ),
]


def stub(reply: dict[str, Any] | None, seen: dict[str, Any] | None = None):
    """A model that answers with whatever it is told to answer with."""

    def call(*, system: str, message: str, tool: dict[str, Any]) -> dict[str, Any] | None:
        if seen is not None:
            seen["system"] = system
            seen["message"] = message
            seen["tool"] = tool
        return reply

    return call


class TestWhatIsSent:
    def test_the_question_and_the_passages_both_go(self) -> None:
        seen: dict[str, Any] = {}
        ask("What were total net sales?", PASSAGES, stub({"answered": False}, seen))
        assert "What were total net sales?" in seen["message"]
        assert "416,161" in seen["message"]

    def test_passages_are_numbered_so_one_can_be_named(self) -> None:
        message = build_message("anything", PASSAGES)
        assert "[passage 12" in message and "[passage 3" in message

    def test_the_model_is_forced_through_the_tool(self) -> None:
        # a tool call returns the shape every time; asking for JSON in
        # prose returns it most of the time, and the checks downstream
        # cannot be built on "most"
        seen: dict[str, Any] = {}
        ask("q", PASSAGES, stub({"answered": False}, seen))
        assert seen["tool"]["name"] == "give_answer"
        assert set(seen["tool"]["input_schema"]["required"]) == {
            "answered",
            "answer",
            "quote",
            "passage_index",
        }

    def test_the_instruction_to_answer_only_from_the_passages_is_sent(self) -> None:
        seen: dict[str, Any] = {}
        ask("q", PASSAGES, stub({"answered": False}, seen))
        assert "ONLY from the passages" in seen["system"]


class TestShapingTheReply:
    def test_carries_the_answer_and_its_quote_through(self) -> None:
        answer = ask(
            "What were total net sales?",
            PASSAGES,
            stub(
                {
                    "answered": True,
                    "answer": "Total net sales were 416,161 million.",
                    "quote": "Total net sales  416,161  391,035  383,285",
                    "passage_index": 12,
                }
            ),
        )
        assert answer.answered
        assert answer.text == "Total net sales were 416,161 million."
        assert answer.passage_index == 12

    def test_looks_up_the_section_of_the_passage_cited(self) -> None:
        # so the answer can say where in the document it came from
        answer = ask(
            "q", PASSAGES, stub({"answered": True, "answer": "x", "quote": "y", "passage_index": 3})
        )
        assert answer.section == "Item 1"

    def test_leaves_the_section_empty_when_the_index_is_not_one_it_was_given(self) -> None:
        # a model naming a passage it was never shown is a fabrication,
        # and inventing a section for it would dress that up
        answer = ask(
            "q",
            PASSAGES,
            stub({"answered": True, "answer": "x", "quote": "y", "passage_index": 999}),
        )
        assert answer.section is None

    def test_arrives_unverified(self) -> None:
        # nothing may be shown or counted until the verifier has run, so
        # the field starts empty rather than optimistic
        answer = ask(
            "q",
            PASSAGES,
            stub({"answered": True, "answer": "x", "quote": "y", "passage_index": 12}),
        )
        assert answer.verified is None


class TestWhenThereIsNoAnswer:
    def test_says_so_when_nothing_was_retrieved(self) -> None:
        # and does not call the model, because there is nothing to ask about
        called = False

        def never(*, system: str, message: str, tool: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal called
            called = True
            return None

        answer = ask("something unrelated", [], never)
        assert not answer.answered
        assert not called

    def test_says_so_when_the_model_returns_nothing(self) -> None:
        answer = ask("q", PASSAGES, stub(None))
        assert not answer.answered
        assert "did not return" in answer.text

    def test_passes_through_the_model_declining_to_answer(self) -> None:
        # declining is a correct answer, and the most valuable one the
        # model can give when the passages do not contain the fact
        answer = ask(
            "What is the chief executive's shoe size?",
            PASSAGES,
            stub(
                {
                    "answered": False,
                    "answer": "The passages do not state that.",
                    "quote": "",
                    "passage_index": -1,
                }
            ),
        )
        assert not answer.answered
        assert answer.quote == ""


class TestPassageLookup:
    def test_finds_the_passage_an_answer_claims_to_have_used(self) -> None:
        found = passage_by_index(PASSAGES, 12)
        assert found is not None and found.section == "Item 7"

    def test_returns_nothing_for_an_index_that_was_never_offered(self) -> None:
        assert passage_by_index(PASSAGES, 999) is None


class TestAnswerModel:
    def test_the_verified_flag_stays_out_of_serialised_output(self) -> None:
        # it is internal bookkeeping, not part of what a caller receives
        answer = Answer(answered=True, text="x", quote="y", passage_index=1, verified=True)
        assert "verified" not in answer.model_dump()
