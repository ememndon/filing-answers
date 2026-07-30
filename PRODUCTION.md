# Getting this into production

Most AI prototypes do not fail because the model was not good enough.
They fail at the handover, and they fail on questions that have nothing
to do with machine learning.

I have watched the same conversation happen more than once. The demo goes
well. Everyone agrees it is impressive. Then somebody senior asks how
we would know if it started getting things wrong, and the room goes
quiet, and the project quietly does not happen. Not because anyone
decided against it — because nobody could answer.

This document is the list of questions that stop a prototype, and what
this repository does about each one. Eight questions. Seven have code
behind them and one does not, and the eighth is named here rather than
left out.

---

## 1. How do we know it is right?

**The problem.** A demo is a handful of questions someone already knows
the answers to. That tells you the thing works on those questions, and
nothing at all about the next thousand. You cannot ship what you cannot
measure, and "it seemed good in the demo" is not a measurement.

**Here.** 48 questions over three real annual reports, with known
answers, in [`evaluation/questions.json`](evaluation/questions.json).
Every expected value was read out of the filing and the line it came
from is recorded beside it, so the answer key can be audited rather
than believed. A benchmark whose own answers cannot be checked measures
nothing.

The set is deliberately awkward, because a set built to be passed is
theatre:

- Prior-year figures are asked for as often as current ones. A financial
  statement puts three years side by side in one row, and picking the
  wrong column is the commonest way to be confidently wrong about a
  filing.
- Where a company reports a number twice, both are asked for. BlackRock
  publishes net income under accounting rules and again "as adjusted",
  two billion dollars apart. Only one is the answer.
- Six questions have no answer in the filing at all. Apple has not
  disclosed iPhone unit sales since 2018; no annual report forecasts the
  year after next. Saying so is the correct answer, and a system that
  cannot say it is the dangerous kind.

```
make evaluate

  questions             48
  answered correctly    46  (95.8%)
  unsupported figures    0
  threshold             85.0%

  PASS
```

## 2. How do we know it is *still* right next month?

**The problem.** Nothing in the repository changed and the answers got
worse anyway. The model was updated underneath you, or the documents
moved, or a dependency shifted. This kind of regression never appears in
a commit, so nothing that only watches commits will ever see it.

**Here.** The gate runs weekly on a schedule as well as on pushes to
`main`, in [`.github/workflows/ci.yml`](.github/workflows/ci.yml). Every
run appends a row — date, model, accuracy, unsupported figures, pass or
fail — to a DuckDB file via [`history.py`](src/filing_answers/history.py).

One run answers "may this ship". A column of runs answers "is this
getting worse", which is the question you would much rather ask early.

## 3. What happens when it is wrong?

**The problem.** This is the one that decides whether a regulated firm
can use the thing at all. A model asked for a number it cannot find will
supply one, in exactly the tone it uses when it is right. A prototype
displays that. Production must not.

The usual answer is a confidence score or a warning banner, and it does
not work. An answer shown with a caveat attached gets read; the caveat
does not.

**Here.** Every figure in an answer is checked against the sentence the
answer cited, mechanically, in [`verify.py`](src/filing_answers/verify.py).
An answer that fails is **withheld** — not flagged, not greyed out, not
returned with a lower score. The caller gets a refusal and the reason,
and the text that failed never leaves the process.

That is enforced one layer below the web service, in
[`pipeline.py`](src/filing_answers/pipeline.py), so the API cannot get it
wrong by forgetting. There is a test whose only job is to assert the
rejected text is absent from the response body.

The check is plain Python with no model in it. It cannot be talked round
by a confident tone, which is the entire reason it is not a prompt.

## 4. Can somebody trace a number back to its source?

**The problem.** "The system said so" is not an audit trail. Anyone
acting on a figure — a risk committee, a client-facing team, a regulator
— needs to get from the number to the place in the document it came
from, without taking anyone's word for it.

**Here.** Every answer carries the sentence it came from and the item of
the filing that sentence is in:

```
  Total revenue in 2025 was $24,216 million.

  "Total revenue  24,216  20,407"
  — BLK 10-K FY2025, Item 7
```

That citation is not decoration. It is the same thing the verifier checks
against, so the evidence shown to a reader and the evidence used by the
machine are one object and cannot drift apart.

Getting the section right took real work — a 10-K prints its financial
statements after Item 15, which naively attributes every figure in the
accounts to "Item 16. Form 10-K Summary", a section whose entire content
is the words "Not applicable". The commit history has the details.

## 5. Can someone who did not build it run it?

**The problem.** The prototype runs on one laptop, with a notebook open,
and the person who wrote it in the room. Nobody else can start it, and
nobody wants to own it.

**Here.**

- **Configuration is validated at startup**, in
  [`config.py`](src/filing_answers/config.py). A misconfigured deployment
  refuses to start rather than starting, reporting healthy, and failing
  on its first real request.
- **Liveness and readiness are separate endpoints**, because they have
  different consequences. A process that is alive but not ready should
  stop receiving traffic, not be killed and restarted.
- **A container that runs as a non-root user**, built in two stages so
  the shipped image carries no compiler, with no `.env` inside it.
  Configuration arrives as environment variables, which is what keeps a
  key out of an image that gets pushed to a registry.
- **Structured logs**, one line per answer, carrying whether it verified
  and how long it took.

## 6. What does it cost, and what is the ceiling?

**The problem.** A prototype has no spending limit and does not need one.
The moment the same code has an address, it is an endpoint converting
anonymous requests into charges on somebody's card, and it will be found.

**Here.** [`limits.py`](src/filing_answers/limits.py) has two limits,
protecting different things: a per-visitor hourly allowance so no single
caller can monopolise it, and a hard daily ceiling that binds however
many callers there are.

The second is the one that matters. A per-caller limit is defeated by
using more callers; only a global ceiling can promise that tomorrow's
invoice is bounded. A refused question never reaches the model, so it
costs nothing, and refusals are not counted against the day — otherwise
a caller hammering a closed door would keep it closed for everybody long
after the real traffic stopped.

## 7. Who decides it may ship, and on what evidence?

**The problem.** In most places this is a meeting. Someone demonstrates
the new version, everyone nods, it goes out. That works until the day it
does not, and nobody can reconstruct what was known at the time.

**Here.** It is an exit code.

```
0  the answers were good enough to ship
1  they were not — block the release
2  the model named does not exist
3  it could not run at all, so nothing was measured
```

Two of those matter more than they look.

`1` is the gate doing its job, and CI has no `continue-on-error`. A gate
that reports a failure while letting the tick go green is a notification,
and nobody reads notifications.

`3` exists because of a mistake this repository actually made. Run in CI
without an API key configured, the gate crashed on startup and exited
`1` — the same code meaning "quality has dropped". A red tick that means
"somebody forgot a secret" is indistinguishable from one meaning "the
model got worse", and the two want opposite responses. Not being able to
measure something is not the same as measuring it and finding it
wanting.

The threshold itself is configuration, not a constant. It is a judgement
about how much being wrong costs, and that judgement belongs to whoever
is deploying it.

**The demonstration this is all for:** make the system worse in a
completely reasonable way. Show the model two passages instead of eight
— every prompt shorter, every request cheaper. `make check` stays green,
because none of it is a bug.

```
make degraded

  answered correctly    30  (62.5%)
  unsupported figures    2

  FAIL — release blocked
```

Sixteen right answers gone and two invented figures in front of a
reader, from a change that looks like housekeeping. The only thing
between that and production is that exit code.

## 8. How does it get handed over and owned?

**Not built.**

Everything above is mechanism, and mechanism is the part an engineer can
finish alone. This one is not: it is ownership, an on-call rotation, a
runbook that says what breaks and who to wake, a decision about which
team carries it after the people who built it move on. Those are agreed
between teams, not written by one person into a repository, and inventing
a version of them here would be furniture rather than work.

What can be said honestly is what is already true: the service starts
from environment variables, refuses to start when they are wrong, tells
you whether it is alive and whether it is ready, logs one structured line
per answer, and has a command that says in one number whether the current
version is fit to ship. That is most of what a receiving team asks for.
The rest is a conversation.

---

## What this does not claim

It does not deploy into anybody's infrastructure, and it could not — the
whole point is that the questions above are answered *before* that
conversation, not during it.

Nor is it a benchmark. 48 questions over three filings, hand-checked, is
a release gate and not a research dataset. It is also honest about its
own limits: one question in the set has never once passed. Asked which
firm audits BlackRock, the answer is a signature — `/s/ Deloitte &
Touche LLP` — sitting in a passage that shares almost no words with the
question, while the auditor's report matches every word of it and never
names anybody. No amount of tuning the search reaches it, and it stays
in the set, failing, because a question set you are allowed to delete
from measures whatever you want it to.
