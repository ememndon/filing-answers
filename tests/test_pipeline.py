"""One question in, one checked answer out.

The behaviour under test is not the answering — that is the model's, and
it is stubbed here. It is what the service does with what comes back:
what it releases, what it withholds, and what it refuses to put in front
of a caller.

The test that matters most is the one asserting a rejected answer's text
is absent from the response. Everything else in the project is arranged
to make that line true.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from filing_answers.edgar import Filing
from filing_answers.pipeline import WITHHELD, AnswerService, FilingIndex, Result

FILING = Filing(
    cik="0000320193",
    ticker="AAPL",
    company="Apple Inc.",
    form="10-K",
    filed=date(2025, 10, 31),
    period=date(2025, 9, 27),
    accession="000032019325000079",
    document="aapl-20250927.htm",
)

# Two passages, one plainly about revenue and one plainly not, so the
# ranking has something to get right.
HTML = """
<html><body>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Total net sales increased 6% or $24,975 million during 2025 compared to 2024,
driven by higher Services net sales, and the Company had approximately
166,000 full-time equivalent employees at the end of the year.</p>
<p>Item 1A. Risk Factors</p>
<p>The Company's business, results of operations and financial condition are
subject to a variety of risks and uncertainties, including those relating to
global economic conditions and to interruptions in its supply chain.</p>
</body></html>
"""


class FakeEdgar:
    """EDGAR without EDGAR. The network is not a test fixture."""

    def __init__(self) -> None:
        self.fetches = 0

    def latest_annual_report(self, ticker: str) -> Filing:
        return FILING

    def document_html(self, filing: Filing) -> str:
        self.fetches += 1
        return HTML


def stub(reply: dict[str, Any] | None):
    def call(*, system: str, message: str, tool: dict[str, Any]) -> dict[str, Any] | None:
        return reply

    return call


def service(reply: dict[str, Any] | None) -> tuple[AnswerService, FakeEdgar]:
    edgar = FakeEdgar()
    return AnswerService(edgar, stub(reply)), edgar  # type: ignore[arg-type]


GOOD = {
    "answered": True,
    "answer": "Total net sales increased 6% during 2025.",
    "quote": "Total net sales increased 6% or $24,975 million during 2025 compared to 2024",
    "passage_index": 0,
}


class TestAGoodAnswer:
    def test_comes_back_verified_with_its_citation(self) -> None:
        svc, _ = service(GOOD)
        result, _ = svc.ask("AAPL", "How much did net sales increase in 2025?")
        assert result.verified
        assert result.answer == "Total net sales increased 6% during 2025."
        assert "24,975" in result.citation

    def test_says_which_filing_and_which_item_it_came_from(self) -> None:
        svc, _ = service(GOOD)
        result, _ = svc.ask("AAPL", "How much did net sales increase in 2025?")
        assert result.source.startswith("AAPL 10-K FY2025")
        assert "Item 7" in result.source

    def test_cites_the_sentence_rather_than_the_whole_passage(self) -> None:
        # a reader checking an answer wants the line, not the page
        svc, _ = service(GOOD)
        result, _ = svc.ask("AAPL", "How much did net sales increase in 2025?")
        assert result.citation == GOOD["quote"]


class TestAnAnswerThatFailsTheCheck:
    BAD = {
        "answered": True,
        "answer": "Total net sales were $500,000 million during 2025.",
        "quote": "Total net sales increased 6% or $24,975 million during 2025 compared to 2024",
        "passage_index": 0,
    }

    def test_is_not_released(self) -> None:
        svc, _ = service(self.BAD)
        result, _ = svc.ask("AAPL", "What were total net sales?")
        assert not result.verified
        assert result.rejected_because

    def test_does_not_carry_the_unsupported_text_to_the_caller(self) -> None:
        # the line this project exists to make true. An answer handed over
        # with a flag attached gets read; the flag does not.
        svc, _ = service(self.BAD)
        result, _ = svc.ask("AAPL", "What were total net sales?")
        assert "500,000" not in result.answer
        assert result.answer == WITHHELD

    def test_names_the_figure_only_as_the_thing_that_was_wrong(self) -> None:
        # the rejection reason does say "500,000", and should: whoever is
        # looking at a blocked answer needs to know which figure failed.
        # What matters is where it appears — in a sentence saying the
        # filing does not contain it, never in the answer.
        svc, _ = service(self.BAD)
        result, _ = svc.ask("AAPL", "What were total net sales?")
        assert result.rejected_because == ["the citation does not contain 500,000"]

    def test_keeps_it_where_the_gate_can_still_count_it(self) -> None:
        # withheld from the caller, not from the evaluation
        svc, _ = service(self.BAD)
        result, _ = svc.ask("AAPL", "What were total net sales?")
        assert "500,000" in result.withheld_text

    def test_says_why(self) -> None:
        svc, _ = service(self.BAD)
        result, _ = svc.ask("AAPL", "What were total net sales?")
        assert "500,000" in " ".join(result.rejected_because)


class TestDecliningToAnswer:
    DECLINED = {
        "answered": False,
        "answer": "The passages do not state how many employees are based in Ireland.",
        "quote": "",
        "passage_index": -1,
    }

    def test_passes_through_as_the_answer_it_is(self) -> None:
        # "the filing does not say" is correct and useful, and is not a
        # failure to be withheld
        svc, _ = service(self.DECLINED)
        result, _ = svc.ask("AAPL", "How many employees are based in Ireland?")
        assert result.verified
        assert "do not state" in result.answer

    def test_claims_no_source_it_does_not_have(self) -> None:
        svc, _ = service(self.DECLINED)
        result, _ = svc.ask("AAPL", "How many employees are based in Ireland?")
        assert result.citation == "" and result.source == ""

    def test_says_so_without_calling_the_model_when_nothing_ranks(self) -> None:
        # a question sharing no words with the document has no passages to
        # answer from, and paying a model to discover that is waste
        svc, _ = service(None)
        result, _ = svc.ask("AAPL", "quixotic bicycle marmalade")
        assert result.verified
        assert "No passage" in result.answer


class TestTheTrace:
    def test_records_the_filing_the_passages_and_the_verdict(self) -> None:
        svc, _ = service(GOOD)
        _, trace = svc.ask("AAPL", "How much did net sales increase in 2025?")
        assert trace.filing is not None and trace.filing.ticker == "AAPL"
        assert trace.considered
        assert trace.verdict is not None and trace.verdict.verified
        assert trace.raw is not None

    def test_keeps_the_raw_answer_even_when_it_was_withheld(self) -> None:
        svc, _ = service(TestAnAnswerThatFailsTheCheck.BAD)
        _, trace = svc.ask("AAPL", "What were total net sales?")
        assert trace.raw is not None and "500,000" in trace.raw.text


class TestParsingOncePerFiling:
    def test_a_second_question_reuses_the_parsed_document(self) -> None:
        # a 10-K is megabytes of Inline XBRL; parsing it per question would
        # make a forty-eight question evaluation unusable
        svc, edgar = service(GOOD)
        svc.ask("AAPL", "one question")
        svc.ask("AAPL", "another question")
        assert edgar.fetches == 1

    def test_the_ticker_is_not_case_sensitive(self) -> None:
        svc, edgar = service(GOOD)
        svc.ask("aapl", "one question")
        svc.ask("AAPL", "another question")
        assert edgar.fetches == 1


class TestIndexing:
    def test_a_filing_becomes_searchable_passages(self) -> None:
        built = FilingIndex(FILING, HTML)
        assert len(built.passages) >= 2
        top = built.search("How much did total net sales increase?")
        assert top and "24,975" in top[0].passage.text


class TestTheResultShape:
    def test_the_withheld_text_stays_out_of_serialised_output(self) -> None:
        result = Result(answer="x", verified=False, withheld_text="a made-up number")
        assert "withheld_text" not in result.model_dump()
