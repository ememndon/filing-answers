# filing-answers

Answers questions about a company's annual report, quotes the sentence the
answer came from, and **refuses to release a version that starts making
things up**.

The interesting part is not the answering. It is everything around it.

---

## The problem

A US annual report runs to several hundred pages. Somewhere in it is the
number you need. A language model can find it in seconds, which is why
every firm is now pointing one at their documents.

Language models also invent numbers. Confidently, in the same tone they
use when they are right. For a company acting on those answers, a made-up
revenue figure that reads plausibly is worse than no answer at all.

So the question this project is really about is not *can a model read a
filing* — it can — but **how do you ship one and be able to sleep**.

## What it does

Ask a question about a filing:

```
$ make ask TICKER=BLK Q="What was total revenue in 2025?"
```

```
  Total revenues in 2025 were $24,216 million.

  "Total revenue  24,216  20,407"
  — BLK 10-K FY2025, Item 7
```

Every answer carries the sentence it came from, and the item of the filing
that sentence is in. That is not a nicety — it is what makes the next part
possible.

## The part that matters

Every number in the answer is checked against the passage it cited. If the
model states a figure that does not appear in its own source, the answer is
withheld — not flagged, not shown with a warning. Withheld. An unsupported
figure handed over with a caveat attached gets read; the caveat does not.

That check also runs over 48 questions with known answers, as a release
gate:

```
$ make evaluate

  questions             48
  answered correctly    46  (95.8%)
  unsupported figures    0
  threshold             85.0%

  PASS
```

Now make the system worse in an entirely reasonable way. Show the model two
passages instead of eight: every prompt gets shorter, every request gets
cheaper, and nothing breaks. It passes the linter, the type checker and all
177 unit tests, because none of that is a bug.

```
$ make degraded

  questions             48
  answered correctly    30  (62.5%)
  unsupported figures    2
  threshold             85.0%

  FAIL — release blocked
```

Sixteen right answers gone and two invented figures in front of a reader,
from a change that looks like housekeeping. That is the failure this is
built to catch, and the only thing standing between it and production is
that command's exit code.

The same check runs in CI. A model that is wrong is a normal Tuesday; a
model that is wrong and ships is an incident.

## Running it

```bash
git clone https://github.com/ememndon/filing-answers.git
cd filing-answers
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then fill in the values
pytest
```

Everything runs locally. Filings are fetched from the SEC's public EDGAR
service, which is free and needs no account — only a contact address in
the request, which the SEC requires.

## How it is built

| Piece | Choice |
|---|---|
| Language | Python 3.12 |
| Service | FastAPI |
| Pipeline | Airflow |
| Storage | Postgres for state, DuckDB for the evaluation history |
| Packaging | Docker |
| Automation | GitHub Actions |
| Telemetry | OpenTelemetry traces, structured logs |
| Model | a small hosted one — deliberately |

The model is the least important choice here. Swapping it is a line of
config, and the gate is what decides whether the swap is allowed to ship.

## What this is not

It is a small, complete example rather than a large, unfinished one.

- **Not a product.** One document type, one question shape, no user accounts.
- **Not a benchmark.** The evaluation set is 48 hand-written questions, not
  a research dataset.
- **Not deployed.** It is built to deploy — twelve-factor config, a proper
  container, health and readiness endpoints — and runs under Docker rather
  than on a cloud account, because a live URL nobody is paying to maintain
  is a broken link waiting to happen.

## Licence

MIT. See [LICENSE](LICENSE).
