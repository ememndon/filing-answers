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
$ curl -s localhost:8000/ask -d '{
    "ticker": "AAPL",
    "question": "What was total net sales in fiscal 2024?"
  }'
```

```json
{
  "answer": "Total net sales were $391,035 million in fiscal 2024.",
  "citation": "Total net sales increased 2% or $7,857 million during 2024
               compared to 2023, to $391,035 million.",
  "source": "AAPL 10-K FY2024, Item 7",
  "verified": true
}
```

Every answer carries the sentence it came from. That is not a nicety — it
is what makes the next part possible.

## The part that matters

Every number in the answer is checked against the passage it cited. If the
model states a figure that does not appear in its own source, the answer is
rejected before anyone sees it.

That check also runs over a fixed set of questions with known answers, as a
release gate:

```
$ make evaluate

  questions            48
  answered correctly   44  (91.7%)
  unsupported figures   0
  threshold            85.0%

  PASS
```

Drop the model's quality and the gate stops the release:

```
  answered correctly   29  (60.4%)
  unsupported figures   6
  threshold            85.0%

  FAIL — release blocked
```

The same check runs in CI, so a change that quietly makes answers worse
cannot merge. A model that is wrong is a normal Tuesday; a model that is
wrong and ships is an incident.

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
