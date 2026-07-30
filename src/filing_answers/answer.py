"""Asking a model a question about a filing, and making it show its source.

The model is given passages and told to answer from them alone. That
instruction is necessary and nowhere near sufficient — a model asked for
a number it cannot find will frequently produce one anyway, in the same
even tone it uses when it is right. So the answer is required to carry
the sentence it came from, and a later step checks the two against each
other.

The quote is not decoration. It is the thing that makes the answer
checkable, by a person reading the card and by the machine that decides
whether a release may ship. An answer without one is discarded.

The reply arrives through a tool call rather than as JSON in prose. A
model asked for JSON returns it most of the time; a model given a tool
returns the shape every time, and "most of the time" is not a foundation
for a check that other things depend on.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from .extract import Passage
from .retrieve import Scored, context_for

SYSTEM_PROMPT = """You answer questions about a company's annual report, \
using only the passages you are given.

Rules, in order of importance:

1. Answer ONLY from the passages provided. They are the entire world. If \
the answer is not in them, say so — that is a correct and useful answer, \
and inventing one is the single worst thing you can do here.

2. Quote the sentence you used, copied EXACTLY from the passage, character \
for character. Do not tidy it, shorten it, join two sentences, or fix its \
punctuation. Your quote is checked against the passage; an answer whose \
quote cannot be found is thrown away.

3. Copy figures exactly as the filing writes them. If it says "391,035", \
write "391,035" — not 391035, not $391 million, not "roughly 391 billion". \
Every figure in your answer is checked against the passage you cited.

4. Do not convert units. A filing reporting in millions says "32,488"; \
writing that as "$32.488 billion" is arithmetic you were not asked to do, \
and the figure no longer matches the document. Give the number as printed \
and let the reader hold the units.

5. Do not add a second figure your quote does not contain. If a company \
reports a number twice — once under accounting rules and once "as \
adjusted" — answer with the one your citation supports and stop. Mentioning \
the other is helpful and unverifiable, and the answer will be discarded.

6. Say which passage number you used.

7. Keep the answer to one or two sentences. State what was asked and stop; \
a reader who wants the context will read the quote.

If several passages could answer, choose the one whose numbers are clearest \
and cite that. If the passages disagree, say so rather than picking."""

ANSWER_TOOL = {
    "name": "give_answer",
    "description": "Answer the question from the passages, with the sentence it came from.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answered": {
                "type": "boolean",
                "description": "False when the passages do not contain the answer.",
            },
            "answer": {
                "type": "string",
                "description": (
                    "One or two sentences. When answered is false, a short statement "
                    "of what the passages do not say."
                ),
            },
            "quote": {
                "type": "string",
                "description": (
                    "The sentence supporting the answer, copied exactly from a passage. "
                    "Empty when answered is false."
                ),
            },
            "passage_index": {
                "type": "integer",
                "description": "The number of the passage quoted. -1 when answered is false.",
            },
        },
        "required": ["answered", "answer", "quote", "passage_index"],
    },
}


class Answer(BaseModel):
    """What the model said, before anything has been verified."""

    answered: bool
    text: str
    quote: str = ""
    passage_index: int = -1
    section: str | None = None

    #: Filled in by the verifier. Nothing should be shown to a person, or
    #: counted in an evaluation, while this is still None.
    verified: bool | None = Field(default=None, exclude=True)


class ModelCall(Protocol):
    """The one thing this module needs from a language model.

    Narrow on purpose: a protocol this small can be satisfied by a stub in
    a test, which means the logic around the model is testable without
    spending money or depending on a network.
    """

    def __call__(
        self, *, system: str, message: str, tool: dict[str, Any]
    ) -> dict[str, Any] | None: ...


def anthropic_caller(api_key: str, model: str, timeout: float = 30.0) -> ModelCall:
    """A ModelCall backed by Anthropic, forced through the tool."""
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key, timeout=timeout)

    def call(*, system: str, message: str, tool: dict[str, Any]) -> dict[str, Any] | None:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            tools=[tool],  # type: ignore[arg-type]
            # Forcing the tool is what turns "usually JSON" into "always
            # this shape", which the checks downstream rely on.
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": message}],
        )
        for block in response.content:
            if block.type == "tool_use":
                return dict(block.input)  # type: ignore[arg-type]
        return None

    return call


def build_message(question: str, scored: list[Scored]) -> str:
    return f"""Question: {question}

Passages from the filing:

{context_for(scored)}"""


def ask(question: str, scored: list[Scored], call: ModelCall) -> Answer:
    """Put the question to the model and return what it said.

    Nothing here is trusted. The reply is shaped, not checked: whether the
    quote is real and whether the figures are supported is decided by the
    verifier, deliberately somewhere else, so that the code producing an
    answer is never also the code approving it.
    """
    if not scored:
        return Answer(
            answered=False,
            text="No passage in the filing appears to bear on that question.",
        )

    reply = call(
        system=SYSTEM_PROMPT,
        message=build_message(question, scored),
        tool=ANSWER_TOOL,
    )
    if reply is None:
        return Answer(answered=False, text="The model did not return an answer.")

    index = int(reply.get("passage_index", -1))
    section = next(
        (s.passage.section for s in scored if s.passage.index == index),
        None,
    )
    return Answer(
        answered=bool(reply.get("answered", False)),
        text=str(reply.get("answer", "")).strip(),
        quote=str(reply.get("quote", "")).strip(),
        passage_index=index,
        section=section,
    )


def passage_by_index(scored: list[Scored], index: int) -> Passage | None:
    """The passage an answer claims to have used."""
    return next((s.passage for s in scored if s.passage.index == index), None)
