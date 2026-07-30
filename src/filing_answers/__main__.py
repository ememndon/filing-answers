"""Running the gate from a terminal.

    python -m filing_answers evaluate
    python -m filing_answers evaluate --passages 2
    python -m filing_answers ask AAPL "What were total net sales in fiscal 2025?"

The exit code is the point of the first one. A gate that prints "FAIL"
and returns zero is a report, and continuous integration does not read
reports — it reads exit codes, and a release nobody is prepared to block
is not gated by anything.

Which means the codes have to say different things:

    0  the answers were good enough to ship
    1  they were not — block the release
    2  the model named does not exist
    3  it could not run at all, so nothing was measured

Three is there because of a mistake this made. Run in CI without its API
key configured, the gate crashed on startup and exited 1 — the same code
it uses for "quality has dropped". A red tick that means "somebody forgot
a secret" is indistinguishable from one that means "the model got worse",
and the two want opposite responses. Not being able to measure something
is not the same as measuring it and finding it wanting.

The second is the demonstration, and it is deliberately an ordinary
change rather than an exotic one. Showing the model two passages instead
of eight makes every prompt shorter and every request cheaper, which is a
reasonable-sounding thing to do on a Friday afternoon. It passes the type
checker, it passes the linter, and it passes all one hundred and
seventy-odd unit tests, because nothing about it is a bug.

It also takes the system from 46 questions right out of 48 to 30, and
puts two unsupported figures in front of a reader. The only thing between
that change and production is this command's exit code.
"""

from __future__ import annotations

import argparse
import sys

from anthropic import NotFoundError
from pydantic import ValidationError

from .answer import anthropic_caller
from .config import settings
from .edgar import EdgarClient
from .evaluate import load, render, run
from .history import record
from .pipeline import AnswerService


def build(
    model: str | None = None, passages: int | None = None
) -> tuple[AnswerService, EdgarClient, str]:
    config = settings()
    chosen = model or config.answer_model
    edgar = EdgarClient(config.sec_user_agent, timeout=config.request_timeout_seconds)
    call = anthropic_caller(config.anthropic_api_key, chosen, config.request_timeout_seconds)
    service = AnswerService(edgar, call, passages) if passages else AnswerService(edgar, call)
    return service, edgar, chosen


def evaluate(arguments: argparse.Namespace) -> int:
    config = settings()
    threshold = arguments.threshold if arguments.threshold is not None else config.threshold
    service, edgar, model = build(arguments.model, arguments.passages)

    questions = load()
    shown = f" · {arguments.passages} passages" if arguments.passages else ""
    print(f"\n  {len(questions)} questions · {model}{shown} · threshold {threshold:.1f}%")
    print("  fetching filings and asking...", flush=True)

    try:
        report = run(questions, service, threshold)
    except NotFoundError:
        # A mistyped model name is the commonest way to run this wrong,
        # and a stack trace is a poor way to say so.
        print(f"\n  no such model: {model}\n")
        return 2
    finally:
        edgar.close()

    print(render(report))
    record(report, model)
    # The line the whole thing exists for.
    return 0 if report.passed else 1


def ask(arguments: argparse.Namespace) -> int:
    service, edgar, model = build(arguments.model, arguments.passages)
    try:
        result, _ = service.ask(arguments.ticker, arguments.question)
    except NotFoundError:
        print(f"\n  no such model: {model}\n")
        return 2
    finally:
        edgar.close()

    print(f"\n  {result.answer}\n")
    if result.citation:
        print(f'  "{result.citation}"')
        print(f"  — {result.source}\n")
    if result.rejected_because:
        print(f"  {'; '.join(result.rejected_because)}\n")
    return 0 if result.verified else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filing-answers", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    gate = commands.add_parser("evaluate", help="run the question set and decide whether to ship")
    gate.add_argument("--model", help="answer with this model instead of the configured one")
    gate.add_argument("--threshold", type=float, help="percentage that must be answered correctly")
    gate.add_argument(
        "--passages",
        type=int,
        help="show the model this many passages instead of the configured eight",
    )
    gate.set_defaults(handler=evaluate)

    one = commands.add_parser("ask", help="put a single question to a filing")
    one.add_argument("ticker")
    one.add_argument("question")
    one.add_argument("--model")
    one.add_argument("--passages", type=int)
    one.set_defaults(handler=ask)

    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except ValidationError as misconfigured:
        # Everything this needs is checked on the way up, so a bad
        # configuration arrives here as one error rather than halfway
        # through a run. Reported as itself, and with its own exit code,
        # so nothing downstream mistakes it for a failing evaluation.
        print("\n  cannot run — the configuration is incomplete:\n")
        for problem in misconfigured.errors():
            field = ".".join(str(part) for part in problem["loc"]).upper()
            message = problem["msg"].removeprefix("Value error, ")
            print(f"    {field}: {message}")
        print("\n  Nothing was measured. This is not a failing evaluation.\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
