"""Choosing which passages a question is answered from.

Retrieval decides the answer. A model handed the wrong paragraphs gives
a wrong answer confidently, or fills the gap itself — so these tests are
about the ranking putting the right passage first, and about the two
adjustments that exist because of how these questions are actually
phrased.
"""

from __future__ import annotations

from filing_answers.extract import Passage
from filing_answers.retrieve import Index, context_for, tokenise


def passage(index: int, text: str, section: str | None = None) -> Passage:
    return Passage(text=text, section=section, index=index)


FILING = [
    passage(
        0,
        "Total net sales   416,161   6   391,035   2   383,285\n"
        "Americas net sales increased during 2025 compared to 2024.",
        "Item 7",
    ),
    passage(
        1,
        "The Company is subject to risks relating to supply chain disruption, "
        "component shortages and the concentration of manufacturing in a small "
        "number of geographies.",
        "Item 1A",
    ),
    passage(
        2,
        "The Company had approximately 164,000 full-time equivalent employees as "
        "of September 27, 2025.",
        "Item 1",
    ),
    passage(
        3,
        "Our board of directors has determined that each member of the audit "
        "committee is independent under the applicable listing standards.",
        "Item 10",
    ),
]


class TestTokenise:
    def test_keeps_a_figure_whole(self) -> None:
        # "391,035" must survive as one token or a question quoting it
        # cannot match the passage containing it
        assert "391,035" in tokenise("Total net sales were 391,035 million")

    def test_drops_words_that_appear_in_every_filing(self) -> None:
        # "company" is in every passage of every 10-K ever written
        assert "company" not in tokenise("The Company reported strong results")

    def test_lowercases_so_matching_is_case_blind(self) -> None:
        assert tokenise("Total Net Sales") == tokenise("total net sales")


class TestRanking:
    def test_puts_the_passage_that_answers_the_question_first(self) -> None:
        top = Index(FILING).search("What were total net sales?")[0]
        assert top.passage.index == 0

    def test_finds_the_risk_passage_for_a_risk_question(self) -> None:
        top = Index(FILING).search("What supply chain risks does the company face?")[0]
        assert top.passage.index == 1

    def test_finds_headcount_without_the_word_headcount(self) -> None:
        top = Index(FILING).search("How many employees are there?")[0]
        assert top.passage.index == 2

    def test_returns_nothing_when_no_passage_bears_on_the_question(self) -> None:
        assert Index(FILING).search("What is the airspeed velocity of a swallow?") == []

    def test_respects_the_limit(self) -> None:
        assert len(Index(FILING).search("net sales employees risks", limit=2)) == 2

    def test_orders_ties_by_position_in_the_document(self) -> None:
        # a stable order matters: an evaluation that shuffles its own
        # context between runs cannot tell a real change from noise
        first = [s.passage.index for s in Index(FILING).search("net sales")]
        second = [s.passage.index for s in Index(FILING).search("net sales")]
        assert first == second


class TestFigureQuestions:
    def test_prefers_a_passage_with_numbers_when_asked_for_an_amount(self) -> None:
        # prose about revenue cannot answer "how much", however well its
        # words match
        prose = passage(
            9, "Net sales are discussed at length in the following section of this report."
        )
        index = Index([prose, FILING[0]])
        assert index.search("What were total net sales?")[0].passage.index == 0

    def test_does_not_penalise_prose_for_a_question_with_no_figure_in_it(self) -> None:
        top = Index(FILING).search("What does the board say about auditor independence?")[0]
        assert top.passage.index == 3


class TestContextForTheModel:
    def test_numbers_each_passage_so_the_model_can_name_one(self) -> None:
        text = context_for(Index(FILING).search("total net sales"))
        assert "[passage 0" in text

    def test_labels_the_section_so_a_citation_has_a_place(self) -> None:
        text = context_for(Index(FILING).search("total net sales"))
        assert "Item 7" in text

    def test_says_when_a_passage_has_no_section(self) -> None:
        text = context_for(
            Index([passage(0, "Some text with no item heading above it at all.")]).search("text")
        )
        assert "unlabelled" in text


class TestEmptyDocument:
    def test_searching_nothing_returns_nothing(self) -> None:
        assert Index([]).search("anything") == []

    def test_a_question_of_nothing_but_stop_words_returns_nothing(self) -> None:
        assert Index(FILING).search("the and of it") == []
