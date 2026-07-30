"""Turning filing markup into passages.

Most of these tests exist because the first attempt failed on a real
document in a way the code could not have told me about. A 300-page
annual report came out as one passage, then as none, and the reason each
time was a rule that looked obviously correct in isolation.

The two properties everything else depends on:

  - a sentence stays whole, so a citation can be quoted
  - a figure stays next to the label that names it, and keeps the
    formatting the filing gave it, so it can be checked later
"""

from __future__ import annotations

from filing_answers.extract import (
    MAX_PASSAGE_CHARS,
    Passage,
    _section_of,
    comparable,
    find_passage,
    is_subheading,
    passages,
)


def page(body: str) -> str:
    return f"<html><body>{body}</body></html>"


class TestSentencesStayWhole:
    def test_inline_markup_does_not_break_a_sentence(self) -> None:
        # filings wrap individual words in spans; treating each as its own
        # line turns one quotable sentence into four unusable fragments
        html = page(
            "<p>Total net sales <span>increased</span> <b>2%</b> or "
            "<span>$7,857 million</span> during 2024 compared to 2023, and "
            "the company continued to invest across all of its segments.</p>"
        )
        text = passages(html)[0].text
        assert "Total net sales increased 2% or $7,857 million during 2024" in text

    def test_inline_xbrl_tags_keep_their_numbers_in_the_sentence(self) -> None:
        # ix:nonFraction wraps every figure in a modern filing. Treated as
        # anything but inline, each number is severed from its meaning.
        html = page(
            "<p>Revenue was <ix:nonFraction>391,035</ix:nonFraction> million "
            "for the year, an increase over the prior period which reflected "
            "growth across the majority of the reportable segments.</p>"
        )
        assert "Revenue was 391,035 million for the year" in passages(html)[0].text

    def test_a_block_element_starts_a_new_line(self) -> None:
        html = page("<p>" + "First paragraph. " * 8 + "</p><p>" + "Second one. " * 8 + "</p>")
        text = passages(html)[0].text
        assert "First paragraph." in text
        assert "\n" in text  # they are separate lines, not run together


class TestFiguresKeepTheirLabels:
    def test_a_table_row_stays_on_one_line(self) -> None:
        # the label is in one cell and the figures in others; split them
        # and a number arrives with nothing to say what it counts
        html = page(
            "<table><tr>"
            "<td>Total net sales</td><td>$</td><td>416,161</td><td>391,035</td>"
            "</tr></table>" + "<p>" + "padding to clear the minimum length. " * 4 + "</p>"
        )
        text = passages(html)[0].text
        assert "Total net sales" in text
        line = next(ln for ln in text.split("\n") if "Total net sales" in ln)
        assert "416,161" in line and "391,035" in line

    def test_number_formatting_survives_exactly(self) -> None:
        # a later step checks an answer's figures against the passage by
        # comparing text, so 391,035 must not become 391035 in transit
        html = page(
            "<p>Net sales of $391,035 million and a margin of 46.2% were "
            "reported for the fiscal year then ended, together with other "
            "measures set out in the accompanying statements.</p>"
        )
        text = passages(html)[0].text
        assert "$391,035" in text
        assert "46.2%" in text

    def test_table_content_is_not_collected_twice(self) -> None:
        # the walk reads a table by rows and must then step over it
        html = page(
            "<table><tr><td>Revenue</td><td>391,035</td></tr></table>"
            "<p>" + "padding to clear the minimum length. " * 4 + "</p>"
        )
        assert passages(html)[0].text.count("391,035") == 1


class TestStructure:
    def test_finds_the_item_a_passage_belongs_to(self) -> None:
        html = page(
            "<p>Item 7. Management's Discussion and Analysis</p>"
            "<p>" + "Net sales rose during the period under review. " * 4 + "</p>"
        )
        assert any(p.section == "Item 7" for p in passages(html))

    def test_handles_lettered_items(self) -> None:
        html = page(
            "<p>Item 1A. Risk Factors</p>"
            "<p>" + "The company faces a number of material risks. " * 4 + "</p>"
        )
        assert any(p.section == "Item 1A" for p in passages(html))

    def test_a_new_item_starts_a_new_passage(self) -> None:
        # running two items together would attach a citation to the wrong
        # part of the document
        html = page(
            "<p>Item 7. Management's Discussion</p>"
            "<p>" + "Discussion of results for the year. " * 4 + "</p>"
            "<p>Item 8. Financial Statements</p>"
            "<p>" + "The statements begin on the following page. " * 4 + "</p>"
        )
        sections = [p.section for p in passages(html)]
        assert "Item 7" in sections and "Item 8" in sections
        seven = next(p for p in passages(html) if p.section == "Item 7")
        assert "following page" not in seven.text


class TestPassageSize:
    def test_drops_headings_and_page_furniture(self) -> None:
        # too short to answer anything from
        assert passages(page("<p>29</p><p>Table of Contents</p>")) == []

    def test_splits_before_a_passage_grows_unquotable(self) -> None:
        html = page("<p>" + ("A sentence of quite ordinary length here. " * 200) + "</p>")
        result = passages(html)
        assert len(result) > 1
        assert all(len(p.text) <= MAX_PASSAGE_CHARS + 200 for p in result)

    def test_numbers_the_passages_in_document_order(self) -> None:
        html = page("".join(f"<p>{f'Paragraph number {i} here. ' * 4}</p>" for i in range(5)))
        result = passages(html)
        assert [p.index for p in result] == list(range(len(result)))


class TestRealWorldMarkup:
    def test_survives_a_document_wrapped_in_containers(self) -> None:
        # the failure that produced one passage for a whole annual report:
        # an unrecognised wrapper treated as inline swallows everything
        html = (
            "<html><body><div><section><article>"
            "<p>" + "The first disclosure of the filing. " * 4 + "</p>"
            "<p>" + "The second disclosure of the filing. " * 4 + "</p>"
            "</article></section></div></body></html>"
        )
        assert len(passages(html)) >= 1
        assert "first disclosure" in passages(html)[0].text

    def test_ignores_scripts_and_styles(self) -> None:
        html = page(
            "<style>.x{color:red}</style><script>var a=1;</script>"
            "<p>" + "Actual reading matter in the filing. " * 4 + "</p>"
        )
        text = passages(html)[0].text
        assert "color:red" not in text and "var a" not in text

    def test_ignores_the_hidden_xbrl_header(self) -> None:
        # inline XBRL repeats every visible fact in a hidden block, so
        # leaving it in means every number appears twice
        html = page(
            '<div style="display:none"><span>391,035</span></div>'
            "<p>" + "Net sales were 391,035 million in the period. " * 3 + "</p>"
        )
        assert passages(html)[0].text.count("391,035") == 3  # from the visible text only


class TestFindPassage:
    def build(self) -> list[Passage]:
        return passages(
            page("<p>" + "Total net sales increased two per cent during the year. " * 3 + "</p>")
        )

    def test_finds_the_passage_a_quote_came_from(self) -> None:
        found = find_passage(self.build(), "Total net sales increased two per cent")
        assert found is not None

    def test_forgives_whitespace_a_model_rearranged(self) -> None:
        found = find_passage(self.build(), "Total net sales   increased\n two per cent")
        assert found is not None

    def test_returns_nothing_for_a_quote_that_is_not_there(self) -> None:
        assert find_passage(self.build(), "Revenue fell sharply in every segment") is None

    def test_refuses_a_fragment_too_short_to_identify_anything(self) -> None:
        # a few words match half the document and prove nothing
        assert find_passage(self.build(), "net sales") is None


class TestHeadingsThatAreLong:
    """Items 5, 7, 9 and 12 have long titles, and were being lost.

    The first version of this capped the text after "Item N" at eighty
    characters, which quietly discarded the four longest headings in a
    10-K. Item 7 is the Management's Discussion — the most-quoted section
    of the document — and everything in it was being filed under Item 6.
    """

    def test_recognises_the_management_discussion(self) -> None:
        line = (
            "Item 7  Management's Discussion and Analysis of Financial Condition "
            "and Results of Operations  38"
        )
        assert _section_of(line, "Item 6") == "Item 7"

    def test_recognises_the_other_three_long_ones(self) -> None:
        for line, expected in [
            (
                "Item 5  Market for Registrant's Common Equity, Related Stockholder "
                "Matters and Issuer Purchases of Equity Securities  37",
                "Item 5",
            ),
            (
                "Item 9  Changes in and Disagreements with Accountants on Accounting "
                "and Financial Disclosure  65",
                "Item 9",
            ),
            (
                "Item 12  Security Ownership of Certain Beneficial Owners and "
                "Management and Related Stockholder Matters  68",
                "Item 12",
            ),
        ]:
            assert _section_of(line, None) == expected

    def test_still_refuses_a_whole_section_that_begins_with_its_own_heading(self) -> None:
        # the case the length guard exists for: BlackRock's filing runs
        # all of Item 1C into one 900-word line starting "Item 1C."
        run_on = "Item 1C. Cybersecurity " + "BlackRock manages cyber risk carefully. " * 60
        assert _section_of(run_on, "Item 1") == "Item 1"

    def test_recognises_a_heading_with_nothing_after_it(self) -> None:
        assert _section_of("Item 8", None) == "Item 8"


class TestTheFinancialStatements:
    """A 10-K prints the accounts after Item 15, not under Item 8.

    Read literally that files every figure in the consolidated accounts
    under "Item 16. Form 10-K Summary", whose entire content in
    BlackRock's filing is the words "Not applicable".
    """

    def test_the_auditors_report_returns_to_item_8(self) -> None:
        line = (
            "Opinion on the Financial Statements We have audited the accompanying "
            "consolidated statements of financial condition of BlackRock, Inc."
        )
        assert _section_of(line, "Item 16") == "Item 8"

    def test_so_do_the_statements_and_the_notes(self) -> None:
        for line in [
            "Report of Independent Registered Public Accounting Firm",
            "Consolidated Statements of Income",
            "Consolidated Statement of Financial Condition",
            "Consolidated Statements of Cash Flows",
            "Notes to the Consolidated Financial Statements",
        ]:
            assert _section_of(line, "Item 16") == "Item 8", line

    def test_a_later_item_heading_still_takes_over(self) -> None:
        # returning the accounts to Item 8 must not pin the section there
        assert _section_of("Item 9A  Controls and Procedures", "Item 8") == "Item 9A"


class TestSubheadings:
    """Filings divide items with headings set in capitals.

    Walking past them glued BlackRock's headcount to the end of a passage
    about risk analytics and competition, where no search would find it.
    """

    def test_a_short_line_in_capitals_is_a_heading(self) -> None:
        assert is_subheading("HUMAN CAPITAL")
        assert is_subheading("COMPETITION")
        assert is_subheading("AVAILABLE INFORMATION")

    def test_a_row_of_figures_in_capitals_is_not(self) -> None:
        # the accounts are full of these, and breaking on them would cut
        # financial statements into unreadable fragments
        assert not is_subheading("EMEA  2,819,058  236,157  (8,762  21,922")
        assert not is_subheading("GAAP:  2025  2024  2023  2022  2021")

    def test_ordinary_prose_is_not(self) -> None:
        assert not is_subheading("BlackRock competes with investment management firms.")
        assert not is_subheading("THE COMPANY BELIEVES THAT ITS RESULTS FOR THE YEAR WERE STRONG")

    def test_the_heading_starts_the_passage_it_introduces(self) -> None:
        html = f"""<html><body>
        <p>{"Risk analytics are managed by a dedicated group. " * 4}</p>
        <p>HUMAN CAPITAL</p>
        <p>With approximately 24,900 employees in more than 30 countries as of
        December 31, 2025, BlackRock provides a broad range of services to
        clients in more than 100 countries across the globe and depends on
        its people for its long-term success in every market it operates in.</p>
        </body></html>"""
        found = passages(html)
        holding = [p for p in found if "24,900" in p.text]
        assert len(holding) == 1
        assert holding[0].text.startswith("HUMAN CAPITAL")
        assert "Risk analytics" not in holding[0].text


class TestWhatCountsAsAFigure:
    """A quantity, not a digit.

    Searching for a headcount, the first version of this counted
    "(1) attract, (2) align, (3) support" as a passage full of figures
    and ranked it above one reading "approximately 24,900 employees".
    """

    def test_numbered_list_markers_are_not_figures(self) -> None:
        listy = Passage(
            text="Practices are designed to: (1) attract talent; (2) align incentives; "
            "and (3) support employees across many aspects of their lives.",
            index=0,
        )
        assert not listy.has_figures

    def test_amounts_percentages_and_counts_are(self) -> None:
        for text in [
            "approximately 24,900 employees in more than 30 countries",
            "revenue of $5 million",
            "sales grew 6% during the year",
            "as of December 31, 2025",
        ]:
            assert Passage(text=text, index=0).has_figures, text


class TestTypography:
    """A filing is typeset; a model transcribing it uses a keyboard."""

    def test_a_curly_apostrophe_reads_the_same_as_a_straight_one(self) -> None:
        # the difference that threw away a correct answer about BlackRock's
        # headcount: the filing wrote "Company’s", the model wrote "Company's"
        assert comparable("Of the Company’s employees") == comparable("Of the Company's employees")

    def test_dashes_of_every_width_read_alike(self) -> None:
        assert comparable("2024–2025") == comparable("2024-2025")
        assert comparable("a — b") == comparable("a - b")

    def test_curly_quotation_marks_read_as_plain_ones(self) -> None:
        assert comparable("“AUM”") == comparable('"AUM"')

    def test_words_and_figures_are_left_exactly_as_they_are(self) -> None:
        # the folding must not be able to turn one number into another
        assert comparable("391,035") != comparable("391035")
        assert comparable("net sales rose") != comparable("net sales fell")
