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

import bisect
import math
import re
from collections import Counter

from pydantic import BaseModel

from .extract import QUANTITY, Passage

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


#: How close a word has to sit to a number before the two are plainly
#: about each other. Roughly a line of text either side, which is the
#: distance across which a reader would still see them as one statement.
NEAR_CHARS = 80

#: A term appearing in more than this share of a document's passages
#: describes the document rather than any part of it. In BlackRock's own
#: 10-K, "blackrock" is one of these.
COMMON_TERM_AT = 0.25

WORD = re.compile(r"[a-z]+")


class Scored(BaseModel):
    """A passage and how well it answers the question asked."""

    passage: Passage
    score: float


def stem(word: str) -> str:
    """A crude singular form, so "revenues" and "revenue" are one term.

    This is here because of a specific failure. Asked "what were total
    revenues in 2025?" against BlackRock's annual report, the search
    returned nothing useful and the model correctly refused to answer —
    while the figure sat in a passage headed "Total revenue". One letter
    stood between a good answer and no answer at all.

    Three rules, applied in order, chosen to be read rather than to be
    linguistically right:

      liabilities -> liability     (-ies becomes -y)
      revenues    -> revenue       (a trailing s, unless the word ends ss)
      revenue     -> revenu        (a trailing e)

    The last looks pointless on its own and is the one that makes the
    others work: it collapses the pair rather than the word, so "sales"
    and "sale" both land on "sal" and meet. "Business" and "gross" keep
    their double s and are left alone.

    Correctness here is not the goal and would not be worth the cost of a
    real stemmer. Both the question and the document pass through this
    same function, so all it has to do is fold them the same way.
    """
    if len(word) > 4 and word.endswith("ies"):
        word = f"{word[:-3]}y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def tokenise(text: str) -> list[str]:
    """Words worth matching on, lowercased, numbers kept whole.

    Figures keep their punctuation so "391,035" stays one token and can
    be matched by a question that quotes it. They are never stemmed —
    trimming a character off a number would make two different figures
    look like the same one, which is the exact failure the rest of this
    project exists to catch.
    """
    raw = re.findall(r"[a-z]+|\d[\d,.]*", text.lower())
    kept = [t for t in raw if t not in STOP_WORDS and len(t) > 1]
    return [t if t[0].isdigit() else stem(t) for t in kept]


def answers_beside_a_figure(text: str, terms: set[str]) -> bool:
    """Whether one of the question's own words sits next to a quantity.

    Word counting alone cannot tell these two apart:

        "With approximately 24,900 employees in more than 30 countries"
        "BlackRock works to keep employees informed and engaged"

    Both are about employees; only one answers "how many". Asked that
    question against BlackRock's filing, ranking by frequency put the
    second above the first, because it says the word more often in fewer
    words. The headcount was in the document and never reached the model.

    What separates them is not how often a word appears but what it
    appears beside. A passage that answers a question about an amount has
    the word and the amount in the same breath.

    Measured against six questions on BlackRock's filing, this changes
    the ranking of exactly one — and changes it from rank seven, where
    the model never sees the passage, to rank five, where it does. The
    other five are already answered by word counting alone. A signal that
    matters rarely and decisively is worth keeping; one that fires
    everywhere is not, which is why the terms are filtered first.

    Positions are found once and searched by bisection rather than
    compared pairwise: a row of a financial statement is a hundred
    numbers, and multiplying that by every word of every passage of every
    question is the kind of arithmetic that turns a fast search slow.
    """
    spots = [m.start() for m in QUANTITY.finditer(text)]
    if not spots:
        return False

    for match in WORD.finditer(text.lower()):
        if stem(match.group()) not in terms:
            continue
        at = match.start()
        i = bisect.bisect_left(spots, at)
        for j in (i - 1, i):
            if 0 <= j < len(spots) and abs(spots[j] - at) <= NEAR_CHARS:
                return True
    return False


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

    def discriminating(self, terms: list[str]) -> set[str]:
        """The question's terms that can tell one passage from another.

        Asked "how many employees does BlackRock have?" of BlackRock's
        own annual report, "blackrock" is not a search term, it is the
        title page. It appears in nearly every passage, so a passage
        containing it beside a number is every passage, and using it to
        judge whether a paragraph answers the question boosted the whole
        document equally — which is the same as boosting nothing.

        A term carrying real information is one most of the document does
        not use. Dropping the rest is the same idea BM25 already applies
        by weight, applied here as a yes or no because proximity is a yes
        or no question.
        """
        total = len(self.passages) or 1
        return {t for t in terms if self._document_frequency.get(t, 0) / total < COMMON_TERM_AT}

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
        wanted = self.discriminating(terms) if wants_figure else set()
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
            # no amounts in it, whatever its wording matches. And among
            # the passages that do carry amounts, the one that answers is
            # the one where the amount sits beside the thing asked about.
            if wants_figure:
                score *= 1.6 if passage.has_figures else 0.4
                if wanted and answers_beside_a_figure(passage.text, wanted):
                    score *= 1.8

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
