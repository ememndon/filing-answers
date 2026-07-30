"""Turning a filing into passages worth searching and worth quoting.

This is the step that decides whether everything after it works. A model
can only answer from what it is given, and a citation is only useful if
someone can read it, so the passages have two jobs at once: carry the
facts intact, and be quotable in an answer.

Three things about real filings make that harder than it sounds.

They are Inline XBRL. A 10-K is a machine-readable document that happens
to render as a web page, so most of the markup is tagging and most of the
first hundred kilobytes is not prose at all.

The numbers live in tables. "Total net sales" is in one cell and
"391,035" is in another, and the units are in a header three rows up.
Flatten that carelessly and the number arrives divorced from its label,
which is worse than losing it — a figure with no name attached is the
raw material of a confident wrong answer.

The formatting must survive exactly. A later step checks that every
figure in an answer appears in the passage it cited. That check compares
text, so "391,035" has to stay "391,035" and not become "391035" on the
way through.
"""

from __future__ import annotations

import re

from lxml import etree
from lxml import html as lxml_html
from pydantic import BaseModel, Field

#: Markup that carries no reading matter.
DROP_TAGS = ("script", "style", "head", "meta", "link", "noscript")

#: Tags that sit INSIDE a sentence. Everything not named here starts a new
#: line, which is the safer default: a filing invents wrapper elements
#: freely, and an unrecognised one that splits a line costs a slightly
#: choppy passage, while an unrecognised one that joins lines glues the
#: whole document into a single unquotable block. The first version of
#: this listed block tags instead, forgot <body>, and produced exactly
#: one passage for a 300-page annual report.
#:
#: The ix: tags are Inline XBRL. They wrap the individual numbers inside a
#: sentence, so treating them as anything but inline would separate every
#: figure in the document from the words that give it meaning.
INLINE_TAGS = {
    "span",
    "a",
    "b",
    "i",
    "em",
    "strong",
    "u",
    "s",
    "small",
    "sub",
    "sup",
    "font",
    "code",
    "tt",
    "abbr",
    "cite",
    "q",
    "mark",
    "ins",
    "del",
    "nobr",
    "ix:nonfraction",
    "ix:nonnumeric",
    "ix:continuation",
    "ix:exclude",
}


def _is_inline(tag: str) -> bool:
    """Whether an element belongs inside the line rather than starting one."""
    name = tag.lower()
    if name in INLINE_TAGS:
        return True
    # lxml renders namespaced Inline XBRL tags as {uri}localName
    local = name.rsplit("}", 1)[-1]
    return local in {"nonfraction", "nonnumeric", "continuation", "exclude"}


#: A 10-K is organised into numbered items, and naming the item a passage
#: came from turns "somewhere in a 300-page document" into a citation a
#: reader can check.
ITEM_HEADING = re.compile(r"^item\s+(\d{1,2}[A-C]?)(?:[.:\s–—-]|$)", re.IGNORECASE)

#: A heading is a title, and a title is short. Some filings run a whole
#: section into one line that starts with its own heading, and without a
#: length guard every such line would be read as a fresh heading.
#:
#: The guard was originally on characters, capped at eighty, which is
#: where this went wrong: it silently discarded the four longest headings
#: in a 10-K — Items 5, 7, 9 and 12 — because their titles are simply
#: long. "Item 7. Management's Discussion and Analysis of Financial
#: Condition and Results of Operations" is ninety-seven characters, so
#: the most-quoted section of the entire document went unrecognised and
#: everything in it was filed under the item before it.
#:
#: Words separate the two cases where characters did not. Real headings
#: in a 10-K run to seventeen words at the very most; a section body that
#: happens to begin with its own heading runs to hundreds.
MAX_HEADING_WORDS = 25

#: The financial statements belong to Item 8, and a 10-K does not print
#: them there. Item 8 is a single line saying the accounts appear at the
#: back; the auditor's report, the consolidated statements and the notes
#: then follow Items 15 and 16 physically, at the end of the document.
#:
#: Read literally that puts every figure in the accounts under "Item 16.
#: Form 10-K Summary", a section whose entire content in BlackRock's
#: filing is the words "Not applicable". A revenue figure cited to it is
#: wrong in a way any reader of accounts would spot immediately, so the
#: statements are recognised by their own headings and returned to the
#: item they actually belong to.
#:
#: Matched at the start of a line and not length-limited, because a
#: filing routinely runs the heading straight into the paragraph under
#: it: the auditor's report arrives as one line beginning "Opinion on
#: the Financial Statements We have audited the accompanying...".
FINANCIAL_STATEMENTS = re.compile(
    r"^(?:report of independent registered public accounting firm"
    r"|opinion on the financial statements"
    r"|notes? to (?:the )?consolidated financial statements"
    r"|consolidated statements? of (?:income|operations|financial condition"
    r"|cash flows?|comprehensive income|changes in equity|stockholders))",
    re.IGNORECASE,
)

#: The most words a line in capitals can hold and still be a heading.
MAX_SUBHEADING_WORDS = 8


def is_subheading(line: str) -> bool:
    """Whether a line is a heading below item level, set in capitals.

    A 10-K divides its items with headings a reader can see and the
    markup does not distinguish: COMPETITION, HUMAN CAPITAL, AVAILABLE
    INFORMATION. Walking past them glues unrelated sections into one
    passage, and a passage about three subjects answers questions about
    none of them well.

    That is not hypothetical. BlackRock's headcount — "approximately
    24,900 employees in more than 30 countries" — sat sixteen hundred
    characters into a passage that opened with risk analytics and ran
    through competition before reaching it. Asked how many people
    BlackRock employs, the search returned three passages about
    communications and training instead, because they were about
    employees and nothing else, while the passage holding the answer was
    mostly about something else entirely.

    Figures are what keep this from firing on the accounts. A financial
    statement is full of lines like "EMEA  2,819,058  236,157" that are
    capitalised and short and are rows of data, not headings.
    """
    words = line.split()
    if not 1 <= len(words) <= MAX_SUBHEADING_WORDS:
        return False
    if any(character.isdigit() for character in line):
        return False
    letters = [character for character in line if character.isalpha()]
    return len(letters) >= 3 and all(character.isupper() for character in letters)


#: Passages shorter than this are headings, page numbers and table
#: furniture rather than anything a question could be answered from.
MIN_PASSAGE_CHARS = 80

#: Long enough to hold a whole disclosure, short enough that quoting it
#: back to someone is still a quotation rather than a chapter.
MAX_PASSAGE_CHARS = 2_000


#: A quantity, as opposed to a digit. A figure a question could be
#: answered with is either large, or carries a currency or percent sign:
#: 24,900 · $1.5 · 6% · 2025.
#:
#: The distinction is not pedantry. Searching for a headcount in
#: BlackRock's filing, the first version of this counted "(1) attract,
#: (2) align, (3) support" as a passage full of figures and ranked it
#: above the passage reading "approximately 24,900 employees in more than
#: 30 countries". Numbered list markers are punctuation, and treating
#: them as data pushed the answer off the page.
QUANTITY = re.compile(r"\$\s*\d|\d\s*%|\b\d{2,}\b")


class Passage(BaseModel):
    """A quotable piece of a filing, and where in it this came from."""

    text: str
    section: str | None = Field(
        default=None,
        description='The item this fell under, e.g. "Item 7", when one could be identified',
    )
    index: int = Field(description="Position in the document, so passages can be ordered")

    @property
    def has_figures(self) -> bool:
        """Whether this passage holds a quantity a question could be answered with."""
        return bool(QUANTITY.search(self.text))


def _clean_tree(html_text: str) -> etree._Element:
    """Parse the filing and remove everything that is not reading matter."""
    # Filings are served with an XML declaration; lxml's HTML parser wants
    # bytes when one is present, or it refuses the string outright.
    document = lxml_html.fromstring(html_text.encode("utf-8", errors="replace"))
    for element in document.iter():
        if element.tag in DROP_TAGS:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    # Inline XBRL hides its machine-readable facts in elements marked
    # display:none. They duplicate the visible text, so leaving them in
    # means every number appears twice.
    for hidden in document.xpath('//*[contains(translate(@style, " ", ""), "display:none")]'):
        parent = hidden.getparent()
        if parent is not None:
            parent.remove(hidden)
    return document


def _row_text(row: etree._Element) -> str:
    """One table row, with its cells kept side by side.

    Financial statements put the label in the first cell and the figures
    in the rest. Joining them on a separator keeps "Total net sales" and
    "391,035" in the same line of text, which is what makes the resulting
    passage both readable and checkable.
    """
    cells = [" ".join(cell.itertext()).strip() for cell in row.xpath("./td | ./th")]
    # Filings pad rows with empty cells for column alignment; a row of
    # nothing but padding is not a row.
    cells = [c for c in cells if c and c not in {"$", "%", ")", "("}]
    return "  ".join(cells) if len(cells) > 1 else (cells[0] if cells else "")


def _lines(document: etree._Element) -> list[str]:
    """The document as lines of text, one line per block, tables row by row.

    Walked rather than iterated, for two reasons that both cost a day.

    A flat iteration cannot skip a subtree. Tables have to be read row by
    row and then not read again, and the obvious way to arrange that —
    emptying the table once its rows are taken — mutates the tree the
    iterator is walking. Descending deliberately means a table can simply
    be handled and stepped over.

    Inline markup has to stay inside its sentence. Filings wrap individual
    words in spans, so treating every element as its own line turns
    "Total net sales increased 2% during 2024" into four fragments, none
    of which is quotable. Text is gathered up to the next BLOCK boundary,
    which is where a human would also break the line.
    """
    lines: list[str] = []

    def flush(parts: list[str]) -> None:
        text = " ".join(" ".join(parts).split())
        if text:
            lines.append(text)
        parts.clear()

    def walk(element: etree._Element) -> None:
        # Comments and processing instructions have a callable tag.
        if not isinstance(element.tag, str) or element.tag in DROP_TAGS:
            return

        if element.tag == "table":
            for row in element.xpath(".//tr"):
                text = _row_text(row)
                if text:
                    lines.append(text)
            return  # rows taken; do not descend and collect them again

        parts: list[str] = []
        if element.text:
            parts.append(element.text)

        for child in element:
            if not isinstance(child.tag, str):
                if child.tail:
                    parts.append(child.tail)
                continue
            if _is_inline(child.tag):
                # stays in the sentence, with everything it contains
                parts.append(" ".join(child.itertext()))
            else:
                flush(parts)
                walk(child)
            if child.tail:
                parts.append(child.tail)

        flush(parts)

    walk(document)
    return lines


SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

#: Full stops that do not end a sentence. Filings are dense with these —
#: hardly a paragraph passes without an "Inc." or a "U.S." — and treating
#: one as a sentence break cuts a citation in half at the worst moment.
ABBREVIATIONS = frozenset(
    {
        "inc.",
        "ltd.",
        "co.",
        "corp.",
        "llc.",
        "plc.",
        "l.p.",
        "n.v.",
        "no.",
        "nos.",
        "mr.",
        "ms.",
        "mrs.",
        "dr.",
        "st.",
        "jr.",
        "sr.",
        "u.s.",
        "u.k.",
        "e.g.",
        "i.e.",
        "etc.",
        "vs.",
        "approx.",
        "fig.",
    }
)


def _sentences(line: str) -> list[str]:
    """Split on sentence ends, putting back the ones that were not.

    Python's regex cannot express "a full stop not preceded by any of
    these words" in one pattern, because a lookbehind has to be a fixed
    width and the abbreviations are not. Splitting first and rejoining
    the mistakes is both simpler to read and easier to extend than the
    alternative.
    """
    parts = SENTENCE_END.split(line)
    out: list[str] = []
    for part in parts:
        if out:
            tail = out[-1].rsplit(" ", 1)[-1].lower()
            if tail in ABBREVIATIONS:
                out[-1] = f"{out[-1]} {part}"
                continue
        out.append(part)
    return out


def _split_long(line: str, limit: int) -> list[str]:
    """Break one over-long line at sentence boundaries.

    Filings contain single paragraphs of several thousand characters —
    risk factors especially run to a page without a break. Splitting only
    between lines leaves those as one passage, which is too long to quote
    back to anyone and too long to be a useful piece of context.

    Splitting happens between sentences and never inside one. A citation
    that begins halfway through a clause reads like a misquote even when
    every word of it is accurate.
    """
    if len(line) <= limit:
        return [line]

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in _sentences(line):
        if current and length + len(sentence) > limit:
            chunks.append(" ".join(current))
            current, length = [], 0
        current.append(sentence)
        length += len(sentence) + 1
    if current:
        chunks.append(" ".join(current))

    # A single sentence longer than the limit has no boundary to break on,
    # and is returned whole rather than cut in half.
    return [c for c in chunks if c]


def _section_of(line: str, current: str | None) -> str | None:
    """The item heading a line announces, or the one still in force."""
    stripped = line.strip()
    # Checked before the length guard, because these headings are the ones
    # that arrive with a paragraph attached.
    if FINANCIAL_STATEMENTS.match(stripped):
        return "Item 8"
    if len(stripped.split()) > MAX_HEADING_WORDS:
        return current
    match = ITEM_HEADING.match(stripped)
    if not match:
        return current
    # A 10-K names each item twice: once in the contents at the front, and
    # again where the item actually begins. Both are headings and both are
    # correct, so the later one simply replaces the earlier.
    return f"Item {match.group(1).upper()}"


def passages(html_text: str) -> list[Passage]:
    """Split a filing into passages, each tagged with the item it fell in.

    Paragraph boundaries are respected: a passage never begins or ends
    mid-sentence, because a citation that starts halfway through a clause
    reads like a misquote even when it is accurate.
    """
    document = _clean_tree(html_text)

    section: str | None = None
    buffer: list[str] = []
    buffered = 0
    out: list[Passage] = []

    def flush() -> None:
        nonlocal buffer, buffered
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        if len(text) >= MIN_PASSAGE_CHARS:
            out.append(Passage(text=text, section=section, index=len(out)))
        buffer = []
        buffered = 0

    for raw in _lines(document):
        stripped = raw.strip()
        if not stripped:
            continue

        found = _section_of(stripped, section)
        if found != section:
            # A new item starts a new passage: running two items together
            # would attach a citation to the wrong part of the document.
            flush()
            section = found
        elif is_subheading(stripped):
            # A heading below item level starts one too, and then stays
            # at the head of the passage it opened — which is where it is
            # most useful, since it says what the passage is about.
            flush()

        # One paragraph can be longer than a whole passage, so a line is
        # not necessarily a unit that fits.
        for line in _split_long(stripped, MAX_PASSAGE_CHARS):
            if buffered + len(line) > MAX_PASSAGE_CHARS:
                flush()
            buffer.append(line)
            buffered += len(line) + 1

    flush()
    return out


#: Characters a filing uses and a model, copying it faithfully, does not
#: reproduce. Typesetting puts a curly apostrophe in "the Company's" and
#: an en dash in a range of years; a model transcribing that sentence
#: writes the apostrophe and the hyphen on its keyboard.
#:
#: This cost a correct answer. Asked BlackRock's headcount, the model
#: found it, quoted the sentence word for word, and was refused — the
#: filing had written "Company’s" and the model had written "Company's".
#: One character, and a true answer with a real source was thrown away.
#:
#: Folding these cannot help a model invent anything. Nothing here
#: changes a word, a number or the meaning of a sentence; it only stops
#: a difference in typesetting being mistaken for a difference in fact.
TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‛": "'",
        "ʼ": "'",
        "´": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
    }
)


def comparable(text: str) -> str:
    """Text reduced to what a comparison should actually care about.

    Typography folded, whitespace collapsed, case dropped. Everything
    that survives is a word, a number or a mark that changes meaning.
    """
    return " ".join(text.translate(TYPOGRAPHY).split()).lower()


def find_passage(all_passages: list[Passage], quote: str) -> Passage | None:
    """The passage a quote came from, if any did.

    Whitespace and typography are normalised on both sides because a
    quote that travelled through a model comes back with its line breaks
    rearranged and its curly apostrophes straightened, and neither is the
    kind of difference worth rejecting an answer over.
    """
    needle = comparable(quote)
    if len(needle) < 20:
        return None
    for passage in all_passages:
        if needle in comparable(passage.text):
            return passage
    return None
