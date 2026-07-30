"""Choosing which passages a question should be answered from.

A filing yields a few hundred passages and a question needs perhaps five.
Getting those five right decides the answer: a model given the wrong
paragraphs will either answer from the wrong part of the document or, far
worse, fill the gap itself.

Ranking is BM25 — ordinary keyword scoring, chosen on purpose. Embeddings
would rank a little better on questions phrased loosely, at the cost of a
model call before every search, a vector store to keep in step with the
documents, and a retrieval step nobody can explain when it misbehaves.
This is a few hundred passages of one document, where keyword scoring is
strong, free, instant, and can be read line by line when it gets
something wrong.

Two adjustments earn their place, both from the shape of the questions
this gets asked:

  - a question about an amount should prefer passages containing amounts
  - a phrase that appears intact is worth more than its words scattered
"""

from __future__ import annotations

import math
import re
from collections import Counter

from pydantic import BaseModel

from .extract import Passage

#: Standard BM25 constants: term-frequency saturation, and how strongly to
#: penalise a long passage for having more room to match in.
BM25_K1 = 1.5
BM25_B = 0.75

#: Words carrying no discriminating power in a filing. "Company" appears
#: in every passage of every 10-K ever written.
_STOP_WORDS_TEXT = """
    a an the and or but if of in on at to for with by from as is are was were be been
    it its this that these those which what when where who whom how why do does did
    have has had will would shall should may might can could there their them they we
    our us you your i he she his her not no nor so than then too very
    company companies inc ltd corporation corp
"""

STOP_WORDS = frozenset(_STOP_WORDS_TEXT.split())

#: A question after a figure. Passages with no digits in them cannot
#: answer one, however well their words match.
ASKS_FOR_A_FIGURE = re.compile(
    r"\b(how much|how many|what (?:was|were|is|are) (?:the )?(?:total|net|gross)?|"
    r"amount|revenue|sales|income|profit|loss|margin|cost|expense|cash|debt|assets|"
    r"headcount|employees|number of|percentage|percent|rate)\b",
    re.IGNORECASE,
)


class Scored(BaseModel):
    """A passage and how well it answers the question asked."""

    passage: Passage
    score: float


def tokenise(text: str) -> list[str]:
    """Words worth matching on, lowercased, numbers kept whole.

    Figures keep their punctuation so "391,035" stays one token and can
    be matched by a question that quotes it.
    """
    raw = re.findall(r"[a-z]+|\d[\d,.]*", text.lower())
    return [t for t in raw if t not in STOP_WORDS and len(t) > 1]


class Index:
    """A searchable view of one filing's passages.

    Built once per document and reused across questions, because the
    document statistics are the expensive part and they do not change.
    """

    def __init__(self, passages: list[Passage]) -> None:
        self.passages = passages
        self._tokens = [tokenise(p.text) for p in passages]
        self._lengths = [len(t) for t in self._tokens]
        self._average_length = (sum(self._lengths) / len(self._lengths)) if passages else 0.0
        self._counts = [Counter(t) for t in self._tokens]

        # How many passages each term appears in, which is what makes a
        # rare word count for more than a common one.
        document_frequency: Counter[str] = Counter()
        for tokens in self._tokens:
            document_frequency.update(set(tokens))
        self._document_frequency = document_frequency

    def _inverse_document_frequency(self, term: str) -> float:
        total = len(self.passages)
        seen = self._document_frequency.get(term, 0)
        # The standard BM25 form, kept positive so a term appearing in
        # most passages contributes nothing rather than something negative.
        return math.log(1 + (total - seen + 0.5) / (seen + 0.5))

    def search(self, question: str, limit: int = 5) -> list[Scored]:
        """The passages most likely to contain the answer, best first."""
        if not self.passages:
            return []

        terms = tokenise(question)
        if not terms:
            return []

        wants_figure = bool(ASKS_FOR_A_FIGURE.search(question))
        phrase = " ".join(question.lower().split())

        scored: list[Scored] = []
        for i, passage in enumerate(self.passages):
            counts = self._counts[i]
            length = self._lengths[i] or 1
            score = 0.0
            for term in terms:
                frequency = counts.get(term, 0)
                if not frequency:
                    continue
                numerator = frequency * (BM25_K1 + 1)
                denominator = frequency + BM25_K1 * (
                    1 - BM25_B + BM25_B * length / (self._average_length or 1)
                )
                score += self._inverse_document_frequency(term) * numerator / denominator

            if score == 0.0:
                continue

            # A question about an amount cannot be answered by prose with
            # no amounts in it, whatever its wording matches.
            if wants_figure:
                score *= 1.6 if passage.has_figures else 0.4

            # The question's own words, appearing intact, are a stronger
            # signal than the same words scattered through a paragraph.
            if len(phrase) > 15 and phrase in passage.text.lower():
                score *= 1.5

            scored.append(Scored(passage=passage, score=round(score, 4)))

        scored.sort(key=lambda s: (-s.score, s.passage.index))
        return scored[:limit]


def context_for(scored: list[Scored]) -> str:
    """The chosen passages, laid out for a model to read and cite.

    Each is numbered and labelled with its section, so the model can name
    which one it used and the answer can be traced back to a place in the
    document rather than to the document as a whole.
    """
    blocks = []
    for item in scored:
        where = item.passage.section or "unlabelled section"
        blocks.append(f"[passage {item.passage.index} — {where}]\n{item.passage.text}")
    return "\n\n".join(blocks)
