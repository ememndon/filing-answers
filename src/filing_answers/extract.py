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
ITEM_HEADING = re.compile(
    r"^item\s+(\d{1,2}[A-C]?)\s*[.:–—-]?\s*(.{0,80})$",
    re.IGNORECASE,
)

#: Passages shorter than this are headings, page numbers and table
#: furniture rather than anything a question could be answered from.
MIN_PASSAGE_CHARS = 80

#: Long enough to hold a whole disclosure, short enough that quoting it
#: back to someone is still a quotation rather than a chapter.
MAX_PASSAGE_CHARS = 2_000


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
        """Whether this passage contains anything a reader would call a number."""
        return bool(re.search(r"\d", self.text))


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
    match = ITEM_HEADING.match(line.strip())
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

        # One paragraph can be longer than a whole passage, so a line is
        # not necessarily a unit that fits.
        for line in _split_long(stripped, MAX_PASSAGE_CHARS):
            if buffered + len(line) > MAX_PASSAGE_CHARS:
                flush()
            buffer.append(line)
            buffered += len(line) + 1

    flush()
    return out


def find_passage(all_passages: list[Passage], quote: str) -> Passage | None:
    """The passage a quote came from, if any did.

    Whitespace is normalised on both sides because a quote that travelled
    through a model comes back with its line breaks rearranged, and that
    is not the kind of difference worth rejecting an answer over.
    """
    needle = " ".join(quote.split()).lower()
    if len(needle) < 20:
        return None
    for passage in all_passages:
        if needle in " ".join(passage.text.split()).lower():
            return passage
    return None
