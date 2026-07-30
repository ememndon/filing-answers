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
cheaper, and nothing breaks. `make check` is green — the linter is happy and
every unit test passes — because none of it is a bug.

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
make install
source .venv/bin/activate

cp .env.example .env      # then fill in the two values
make check                # linter and tests, no key needed
make evaluate             # the gate, which does need one
```

Or in a container:

```bash
make docker
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=... -e SEC_USER_AGENT="you you@example.org" filing-answers
```

Filings come from the SEC's EDGAR service, which is free and needs no
account — only a contact address on the request, which they require and
this enforces rather than hopes for.

## How it is built

| Piece | Choice |
|---|---|
| Language | Python 3.12 |
| Service | FastAPI, with liveness and readiness separated |
| Retrieval | BM25 over passages cut from the filing itself |
| Verification | plain Python, no model in the loop |
| Question set | 48 hand-written questions, as JSON beside the code |
| History | DuckDB, one row per evaluation run |
| Logs | structlog, structured |
| Packaging | Docker, non-root, two-stage |
| Automation | GitHub Actions |
| Model | a small hosted one — deliberately |

The model is the least important choice on that list. Swapping it is one
line of config; the gate is what decides whether the swap may ship.

Retrieval is keyword scoring rather than embeddings, on purpose. This is a
few hundred passages of one document, where BM25 is strong, instant, free,
and — when it puts the wrong paragraph first — can be read line by line
until you find out why. Two of the fixes in this repo's history came from
being able to do exactly that.

## What this is not

A small, complete example rather than a large, unfinished one. What is
missing is missing on purpose, and it is worth being specific about it.

- **No orchestrator.** There is no pipeline to schedule. One question is
  one request, and the only recurring job is the weekly evaluation, which
  is four lines of cron in the CI workflow. Airflow to run that would be
  a second system to keep alive in order to avoid a `schedule:` block.
- **No database.** Nothing here has state worth keeping between requests
  except parsed filings, which are a cache and belong in memory. Evaluation
  history is a DuckDB file because it is a file: no server, no migrations,
  and anyone can open it.
- **No tracing.** There is one span worth having and its timing is already
  in the log line. OpenTelemetry earns its keep across service boundaries,
  and this has one service.
- **Not a benchmark.** 48 questions over three filings, hand-checked. It is
  a release gate, not a research dataset, and it is honest about what it
  cannot answer: one question in the set has never passed, and it stays in.
- **Not deployed.** It is built to deploy — twelve-factor config, health and
  readiness, a non-root container — and it runs locally rather than on a
  cloud account, because a live URL nobody is paying to maintain is a broken
  link waiting to happen.

## Licence

MIT. See [LICENSE](LICENSE).
