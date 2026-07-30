"""Marking the answers, and deciding whether a version may ship.

No model runs here. What is being tested is the arithmetic of the gate
itself, because a gate whose own marking is wrong is worse than no gate:
it reports a number, the number is believed, and nobody looks again.

The two rules worth reading are that declining counts as an answer when
declining is correct, and that a single unsupported figure fails a run
outright no matter how high the accuracy is.
"""

from __future__ import annotations

import json

from filing_answers.evaluate import (
    Outcome,
    Question,
    Report,
    grade,
    load,
    questions_path,
    render,
    states_figure,
)
from filing_answers.pipeline import Result


def question(**kwargs) -> Question:
    base = {"ticker": "AAPL", "question": "What were total net sales?"}
    return Question(**{**base, **kwargs})


def answered(text: str) -> Result:
    return Result(answer=text, citation="a real sentence", source="Item 7", verified=True)


def withheld(*reasons: str) -> Result:
    return Result(answer="withheld", verified=False, rejected_because=list(reasons))


class TestStatingAFigure:
    def test_accepts_the_figure_however_it_is_written(self) -> None:
        for text in [
            "Total net sales were 416,161 million.",
            "Total net sales were $416,161 million.",
            "Total net sales were 416161 million.",
        ]:
            assert states_figure(text, "416,161"), text

    def test_rejects_a_different_number(self) -> None:
        assert not states_figure("Total net sales were 391,035 million.", "416,161")

    def test_rejects_a_rounded_one(self) -> None:
        # "$416 million" is not a tidier 416,161, it is a different figure
        assert not states_figure("Total net sales were about $416 million.", "416,161")

    def test_finds_a_figure_among_others(self) -> None:
        assert states_figure("Sales rose 6% from 391,035 to 416,161 million.", "416,161")


class TestMarkingAnAnswer:
    def test_marks_a_right_answer_right(self) -> None:
        correct, why = grade(
            question(figures=["416,161"]),
            answered("Total net sales were 416,161 million."),
            declined=False,
        )
        assert correct and why == ""

    def test_marks_a_wrong_figure_wrong_and_says_which(self) -> None:
        correct, why = grade(
            question(figures=["416,161"]),
            answered("Total net sales were 391,035 million."),
            declined=False,
        )
        assert not correct
        assert "416,161" in why

    def test_requires_every_figure_asked_for(self) -> None:
        correct, why = grade(
            question(figures=["121,000", "223,000"]),
            answered("Microsoft employed 223,000 people."),
            declined=False,
        )
        assert not correct and "121,000" in why

    def test_marks_wording_questions_on_their_wording(self) -> None:
        assert grade(
            question(phrases=["Ernst & Young"]),
            answered("The Company's auditor is Ernst & Young LLP."),
            declined=False,
        )[0]
        assert not grade(
            question(phrases=["Ernst & Young"]),
            answered("The Company's auditor is Deloitte."),
            declined=False,
        )[0]

    def test_ignores_the_case_of_wording(self) -> None:
        assert grade(
            question(phrases=["Cupertino"]),
            answered("Headquarters are in cupertino, California."),
            declined=False,
        )[0]


class TestWhenTheFilingDoesNotSayIt:
    def test_declining_is_the_right_answer_and_is_marked_right(self) -> None:
        correct, _ = grade(question(declines=True), answered("irrelevant"), declined=True)
        assert correct

    def test_answering_anyway_is_marked_wrong(self) -> None:
        # the failure the set exists to catch. Apple has not published
        # iPhone unit sales since 2018, so any figure is invented.
        correct, why = grade(
            question(declines=True),
            answered("The Company sold 232 million iPhones."),
            declined=False,
        )
        assert not correct
        assert "does not answer" in why

    def test_declining_a_question_the_filing_does_answer_is_wrong(self) -> None:
        # refusing everything would otherwise be a perfect strategy
        correct, why = grade(question(figures=["416,161"]), answered(""), declined=True)
        assert not correct
        assert "does answer" in why


class TestAWithheldAnswer:
    def test_counts_as_wrong(self) -> None:
        correct, why = grade(
            question(figures=["416,161"]),
            withheld("the citation does not contain 500,000"),
            declined=False,
        )
        assert not correct
        assert "withheld" in why

    def test_carries_the_reason_into_the_report(self) -> None:
        _, why = grade(question(figures=["1"]), withheld("no sentence was quoted"), declined=False)
        assert "no sentence was quoted" in why


def outcome(correct: bool, unsupported: list[str] | None = None) -> Outcome:
    return Outcome(
        question=question(),
        correct=correct,
        verified=not unsupported,
        unsupported=unsupported or [],
    )


class TestWhetherAVersionMayShip:
    def test_passes_above_the_threshold(self) -> None:
        report = Report(outcomes=[outcome(True)] * 9 + [outcome(False)], threshold=85.0)
        assert report.accuracy == 90.0
        assert report.passed

    def test_fails_below_it(self) -> None:
        report = Report(outcomes=[outcome(True)] * 8 + [outcome(False)] * 2, threshold=85.0)
        assert report.accuracy == 80.0
        assert not report.passed

    def test_one_invented_figure_fails_a_run_that_would_otherwise_pass(self) -> None:
        # the rule that makes this a safety gate rather than a score.
        # Ten out of ten right, one of them stating a number its own
        # citation does not contain, is not a release worth having.
        report = Report(outcomes=[outcome(True)] * 9 + [outcome(True, ["500,000"])], threshold=85.0)
        assert report.accuracy == 100.0
        assert report.unsupported == 1
        assert not report.passed

    def test_counts_what_was_withheld_separately(self) -> None:
        report = Report(
            outcomes=[outcome(True)] * 8 + [outcome(False, ["999"])] * 2, threshold=85.0
        )
        assert report.withheld == 2


class TestTheReport:
    def test_says_pass_when_it_passed(self) -> None:
        text = render(Report(outcomes=[outcome(True)] * 10, threshold=85.0))
        assert "PASS" in text and "FAIL" not in text

    def test_says_why_the_release_is_blocked(self) -> None:
        text = render(Report(outcomes=[outcome(False)] * 10, threshold=85.0))
        assert "FAIL — release blocked" in text

    def test_lists_the_questions_that_failed(self) -> None:
        failed = Outcome(question=question(), correct=False, why="does not state 416,161")
        text = render(Report(outcomes=[failed], threshold=85.0))
        assert "does not state 416,161" in text
        assert "What were total net sales?" in text


class TestTheQuestionSetItself:
    def test_loads(self) -> None:
        assert len(load()) == 48

    def test_every_question_states_what_a_right_answer_is(self) -> None:
        # a question with no expectation cannot be marked, and would
        # silently count as correct
        for q in load():
            assert q.figures or q.phrases or q.declines, q.question

    def test_every_answer_records_where_it_was_read_from(self) -> None:
        # so the answer key can be audited rather than trusted
        for q in load():
            assert q.source, q.question

    def test_covers_more_than_one_filing(self) -> None:
        assert len({q.ticker for q in load()}) >= 3

    def test_includes_questions_the_filings_do_not_answer(self) -> None:
        assert sum(1 for q in load() if q.declines) >= 4

    def test_asks_for_prior_years_as_well_as_the_current_one(self) -> None:
        # picking the wrong column of a three-year row is the commonest
        # way to be confidently wrong about a filing
        raw = json.loads(questions_path().read_text(encoding="utf-8"))
        prior = [q for q in raw["questions"] if "column" in q.get("source", "")]
        assert len(prior) >= 6
