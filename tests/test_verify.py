"""The check the whole project is named for.

These are the tests that decide whether the promise holds. Every one of
them is a way a plausible-sounding answer can be wrong, and the point of
the module is that none of them can be talked round by a confident tone.

The asymmetry throughout: an honest answer wrongly rejected costs a
question; a fabricated one wrongly accepted costs whoever acted on it.
So where the two conflict, the check refuses.
"""

from __future__ import annotations

from filing_answers.answer import Answer
from filing_answers.extract import Passage
from filing_answers.verify import (
    checked,
    figures_in,
    quote_is_real,
    unsupported_figures,
    verify,
)

PASSAGE = Passage(
    text=(
        "Total net sales  416,161  391,035  383,285\n"
        "Total net sales increased 6% or $24,975 million during 2025 compared to 2024, "
        "and the Company had approximately 166,000 full-time equivalent employees."
    ),
    section="Item 7",
    index=12,
)


def answer(text: str, quote: str, index: int = 12, answered: bool = True) -> Answer:
    return Answer(answered=answered, text=text, quote=quote, passage_index=index)


class TestTheQuoteMustBeReal:
    def test_accepts_a_sentence_that_is_in_the_passage(self) -> None:
        assert quote_is_real("Total net sales increased 6% or $24,975 million", PASSAGE)

    def test_forgives_whitespace_a_model_rearranged(self) -> None:
        # a model copying accurately still returns the line breaks
        # differently, and failing that catches no dishonesty at all
        assert quote_is_real("Total net sales\n  increased 6%   or $24,975 million", PASSAGE)

    def test_rejects_a_sentence_nobody_wrote(self) -> None:
        assert not quote_is_real("Total net sales decreased sharply during the year", PASSAGE)

    def test_rejects_a_paraphrase_however_close(self) -> None:
        # "rose" for "increased" is a rewrite, and a rewrite is not a quote
        assert not quote_is_real("Total net sales rose 6% or $24,975 million", PASSAGE)

    def test_rejects_a_fragment_too_short_to_identify_anything(self) -> None:
        # a handful of common words appears all over a filing and proves
        # only that the model can echo ordinary language
        assert not quote_is_real("net sales", PASSAGE)


class TestEveryFigureMustBeInTheSource:
    def test_accepts_figures_that_are_in_the_passage(self) -> None:
        assert unsupported_figures("Net sales were 416,161 million in 2025.", PASSAGE) == []

    def test_does_not_care_how_a_number_is_punctuated(self) -> None:
        # the value is what is being checked, not the typography
        assert unsupported_figures("Net sales were 416161 million.", PASSAGE) == []

    def test_catches_a_figure_that_is_simply_not_there(self) -> None:
        assert unsupported_figures("Net sales were 999,999 million.", PASSAGE) == ["999,999"]

    def test_catches_a_helpfully_rounded_figure(self) -> None:
        # the failure worth catching. "$416 million" is not a tidier
        # version of 416,161 — it is a different number, and a firm acting
        # on it is acting on something the filing never said.
        assert unsupported_figures("Net sales were about $416 million.", PASSAGE) == ["416"]

    def test_catches_arithmetic_the_source_does_not_support(self) -> None:
        # the model adding two real numbers together produces a third that
        # is nowhere in the document
        assert unsupported_figures("The two segments totalled 807,196.", PASSAGE) == ["807,196"]

    def test_reports_each_missing_figure_once(self) -> None:
        assert unsupported_figures("Both 999 and 999 are wrong.", PASSAGE) == ["999"]

    def test_checks_percentages_too(self) -> None:
        assert unsupported_figures("Sales grew 6%.", PASSAGE) == []
        assert unsupported_figures("Sales grew 41%.", PASSAGE) == ["41"]


class TestTheVerdict:
    def test_passes_an_answer_that_is_quoted_and_supported(self) -> None:
        verdict = verify(
            answer(
                "Total net sales were 416,161 million in fiscal 2025.",
                "Total net sales  416,161  391,035  383,285",
            ),
            [PASSAGE],
        )
        assert verdict.verified
        assert verdict.summary == "supported by the citation"

    def test_fails_an_answer_whose_quote_is_invented(self) -> None:
        verdict = verify(
            answer("Net sales were 416,161 million.", "Net sales rose considerably this year."),
            [PASSAGE],
        )
        assert not verdict.verified
        assert "appears in none" in verdict.summary

    def test_fails_an_answer_whose_figure_is_invented(self) -> None:
        verdict = verify(
            answer(
                "Total net sales were 500,000 million.",
                "Total net sales  416,161  391,035  383,285",
            ),
            [PASSAGE],
        )
        assert not verdict.verified
        assert verdict.unsupported_figures == ["500,000"]
        assert "500,000" in verdict.summary

    def test_accepts_an_accurate_quote_labelled_with_the_wrong_passage(self) -> None:
        # the failure this found on Apple's filing: the model quoted the
        # revenue line word for word and then named passage 77 when the
        # sentence was in 96. The quote is the evidence; the index is
        # bookkeeping, and a clerical slip is not a fabrication.
        elsewhere = Passage(
            text="Some other part of the filing entirely.", section="Item 1", index=99
        )
        verdict = verify(
            answer(
                "Total net sales were 416,161 million.",
                "Total net sales  416,161  391,035  383,285",
                index=99,
            ),
            [elsewhere, PASSAGE],
        )
        assert verdict.verified
        assert verdict.misattributed
        assert verdict.source is not None and verdict.source.index == 12

    def test_still_rejects_a_quote_in_none_of_the_passages_offered(self) -> None:
        # forgiving the index must not become forgiving the evidence
        verdict = verify(
            answer("Net sales were 416,161 million.", "A sentence from no document at all here."),
            [PASSAGE],
        )
        assert not verdict.verified

    def test_a_fabricated_quote_fails_on_its_own(self) -> None:
        # and stops there. With no real source there is nothing to check
        # the figures against, and listing a second fault derived from a
        # passage the answer never used would be inventing detail.
        verdict = verify(
            answer("Net sales were 500,000 million.", "Something nobody wrote."), [PASSAGE]
        )
        assert not verdict.verified
        assert verdict.reasons == ["the quoted sentence appears in none of the passages provided"]
        assert verdict.source is None

    def test_fails_an_answer_citing_a_passage_it_was_never_given(self) -> None:
        # a model naming a passage that was not in front of it has
        # fabricated the citation, whatever the answer says
        verdict = verify(answer("Net sales were 416,161.", "Total net sales  416,161"), [])
        assert not verdict.verified
        assert "no passage was provided" in verdict.summary

    def test_passes_an_answer_that_declines_to_answer(self) -> None:
        # saying the filing does not contain something is correct and
        # useful, and there is nothing in it to be unsupported
        verdict = verify(
            answer("The filing does not state that.", "", index=-1, answered=False), []
        )
        assert verdict.verified


class TestRecordingTheVerdict:
    def test_marks_a_good_answer_verified(self) -> None:
        result, verdict = checked(
            answer(
                "Total net sales were 416,161 million.",
                "Total net sales  416,161  391,035  383,285",
            ),
            [PASSAGE],
        )
        assert result.verified is True and verdict.verified

    def test_marks_a_bad_answer_unverified(self) -> None:
        result, verdict = checked(answer("Net sales were 999,999.", "invented sentence"), [PASSAGE])
        assert result.verified is False and not verdict.verified


class TestFigureScanning:
    def test_finds_the_shapes_filings_actually_use(self) -> None:
        found = figures_in("Revenue of $391,035 million rose 46.2% in 2025 against 1.5 prior")
        assert "391,035" in found and "46.2" in found and "2025" in found and "1.5" in found

    def test_finds_nothing_in_prose_with_no_numbers(self) -> None:
        assert figures_in("The Company operates in several geographic segments.") == []


class TestAYearTheQuestionAlreadyNamed:
    """The false rejection that cost a correct answer.

    Asked "what were total assets at the end of fiscal 2025?", the model
    answered "total assets at the end of fiscal 2025 were $359,241
    million" and cited the balance sheet row, which holds the amount and,
    being a row of figures, no year at all. The answer was right, its
    figure was exact, its citation was real, and it was thrown away for
    repeating the date it had been asked about.
    """

    def test_a_year_from_the_question_is_not_a_claim(self) -> None:
        row = Passage(text="Total assets  359,241  364,980", section="Item 8", index=1)
        assert (
            unsupported_figures(
                "Total assets at the end of fiscal 2025 were 359,241 million.",
                row,
                "What were total assets at the end of fiscal 2025?",
            )
            == []
        )

    def test_a_year_the_question_never_mentioned_still_is(self) -> None:
        # a model that dates a figure by itself is making a claim about
        # which year it belongs to, and that has to be supported
        row = Passage(text="Total assets  359,241  364,980", section="Item 8", index=1)
        assert unsupported_figures("Total assets in 1998 were 359,241 million.", row, "") == [
            "1998"
        ]

    def test_an_amount_is_never_let_through_by_this(self) -> None:
        # the rule is for years only, so a question mentioning a number
        # cannot be used to smuggle that number into an answer
        row = Passage(text="Total assets  359,241  364,980", section="Item 8", index=1)
        assert unsupported_figures(
            "Total assets were 500,000 million.",
            row,
            "Were total assets 500,000 million?",
        ) == ["500,000"]

    def test_the_whole_verdict_passes_now(self) -> None:
        row = Passage(text="Total assets  359,241  364,980", section="Item 8", index=1)
        verdict = verify(
            answer(
                "Total assets at the end of fiscal 2025 were 359,241 million.",
                "Total assets  359,241  364,980",
                index=1,
            ),
            [row],
            "What were total assets at the end of fiscal 2025?",
        )
        assert verdict.verified


class TestFiguresAreReadCleanly:
    def test_a_sentence_comma_is_not_part_of_the_year(self) -> None:
        # "the citation does not contain 2025,," is a puzzle, not an
        # explanation
        assert figures_in("As of December 31, 2025, sales rose 6%.") == ["31", "2025", "6"]

    def test_a_thousands_comma_still_is(self) -> None:
        assert figures_in("Revenue of 391,035 million") == ["391,035"]
