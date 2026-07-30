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
    find_passage,
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
