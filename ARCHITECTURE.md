# Why it is built this way

This is not what the code does — the code says that. This is why each
decision was made, what it cost, and the condition under which it
becomes the wrong decision.

Most of these are choices to leave something out. That is the harder
kind to explain later, so it is written down now.

---

## Why no vector database

Because this is a few hundred passages of **one document**, and keyword
scoring is very good at that.

A 10-K becomes around 400 passages. BlackRock's is twelve megabytes of
Inline XBRL and takes about a second to parse into 404 of them. At that
size BM25 is instant, free, and needs nothing kept in sync with anything.

Embeddings would buy better matching on loosely-worded questions. They
would cost a model call before every search, a store that has to stay
consistent with documents that get re-filed, and a retrieval step that
cannot be explained when it misbehaves.

That last one is not theoretical. **Every retrieval bug in this
repository was found by reading the ranking**, and each was invisible
until somebody looked:

- A question about "revenue**s**" could not find a line headed "Total
  revenue". One letter, and the fix is four lines of crude stemming.
- Searching for a headcount, `(1) attract, (2) align, (3) support`
  counted as a passage full of figures and outranked one reading
  "approximately 24,900 employees".
- "BlackRock" appears on nearly every page of BlackRock's own filing, so
  using it as a search term boosted the entire document equally, which is
  arithmetically the same as boosting nothing.

Each of those took minutes to diagnose because the score is a number I
can print. In embedding space they are a cosine distance that is slightly
lower than it should be, for no visible reason.

**What it costs.** Recall was measured, not assumed: across the 42
answerable evaluation questions, the passage holding the answer is in the
top 5 for 38 and the top 8 for 41. Twelve finds nothing more. So the
model is shown eight.

The forty-second is a genuine miss and probably the one embeddings would
win. Asked which firm audits BlackRock, the answer is a signature —
`/s/ Deloitte & Touche LLP` — in a passage sharing almost no words with
the question, while the auditor's report matches every word of it and
names nobody. Keyword scoring cannot bridge that. It stays in the set,
failing.

**When this becomes wrong:** searching across many documents at once, or
questions phrased nothing like the source. See the last section.

## Why this chunking

Passages have two jobs at once, and the second is usually forgotten:
carry the facts intact, **and be quotable in an answer**.

Fixed-size windows fail the second. A 512-token window cuts sentences in
half, and a citation beginning halfway through a clause reads like a
misquote even when every word is accurate. So passages break where the
document itself breaks, never mid-sentence, at boundaries a reader would
also see:

- **Item headings** (`Item 7. Management's Discussion...`), so a citation
  names a section somebody can turn to.
- **Capitalised subheadings** (`HUMAN CAPITAL`, `COMPETITION`), because
  walking past them glues unrelated sections together. BlackRock's
  headcount sat 1,600 characters into a passage that opened with risk
  analytics and ran through competition on the way. It was in the
  document and unreachable.
- **Table rows, kept whole.** Financial statements put "Total net sales"
  in one cell and "416,161" in the next. Flatten that carelessly and the
  number arrives with no label — which is worse than losing it, because a
  figure with no name attached is the raw material of a confident wrong
  answer.

Formatting survives exactly. "391,035" must stay "391,035" and not become
"391035", because the verifier compares text.

**What it costs.** It is format-specific. This understands 10-Ks, and a
different document type needs new rules. That narrowing is deliberate:
one document type done properly is worth more than five done generically,
and the generic version is what produces citations pointing at the wrong
section.

## Why verification *after* generation, not before

Because before generation there is nothing to verify.

The alternatives all sound like they belong earlier: a firmer prompt,
constrained decoding, filtering what retrieval returns. Each reduces how
often a model invents a figure. **None of them detects when it does.**

That is the distinction the whole project rests on. A prompt is a
request. A check is a fact. This repository's system prompt already says
"copy figures exactly as the filing writes them", and during evaluation
the model wrote `$32.488 billion` where the filing said `32,488` — a unit
conversion nobody asked for. The instruction was there. It did not hold.
The check caught it.

Verification afterwards can be mechanical, deterministic and completely
uninterested in how confident the answer sounds. It compares two strings.
There is no version of it that can be persuaded.

It is also **deliberately separate code from the code that produces the
answer** — `answer.py` shapes what the model said and checks nothing;
`verify.py` decides and generates nothing. The thing producing an answer
must never also be the thing approving it.

**What it costs.** You pay for generation even when you throw the result
away. That is the right trade: a wasted model call is cheap, and a
fabricated revenue figure that reads plausibly is not.

## Why the answer is withheld rather than flagged

A confidence score or a warning banner is the usual answer, and it does
not work. **An answer shown with a caveat gets read; the caveat does
not.**

So a failed answer is not returned at all — not in a field, not behind a
flag, not greyed out. The caller gets a refusal and the reason, and the
text that failed never leaves the process. That is enforced in the
pipeline rather than the web layer, so the API cannot get it wrong by
forgetting.

## Why a forced tool call instead of asking for JSON

A model asked for JSON returns it most of the time. A model given a tool
returns the shape every time.

Everything downstream — the verifier, the evaluation, the release gate —
depends on the answer having a quote and a passage number. "Most of the
time" is not a foundation for a check that other things stand on.

## Why the quote is mandatory

It is the load-bearing part. Without it there is nothing to check
against, and every other guarantee here evaporates.

It is also the same object twice over: the sentence shown to a reader as
the citation *is* the sentence the verifier tested. They cannot drift
apart, because there is only one of them.

## Why DuckDB for evaluation history

Because it is a file.

No server to run, no migration to apply, nothing to keep alive between
releases, and anyone with a copy can open it. CI appends one row per run
and uploads the file as an artifact.

The alternative — a table in the service's own database — would tie the
record of *how well the thing works* to the thing working, which is
exactly backwards. And standing up Postgres for one append-only table
read once a week is a second system to operate in order to avoid a file.

**What it costs.** One writer at a time, and no remote querying. For one
CI job appending one row, neither matters.

## Why FastAPI

Because the domain models already exist, so the API layer is nearly free
— and more importantly, **because the safety property lives in the
type**.

`Result.withheld_text` is declared `exclude=True`. The text that failed
verification is physically incapable of being serialised, whatever a
handler does. In a framework where responses are assembled by hand, that
rule would be a line in a request handler that somebody could delete in a
refactor and no test would necessarily notice.

Schema, validation and documentation being one declaration is a
convenience. That is the reason.

## Why the question set is JSON on disk

It is data, not code, so it can be read and argued with by someone who
does not want to read Python — and it needs to be argued with, because
the expected answers are the thing everything else is measured against.

Every entry records the line in the filing it was read from, so the
answer key can be audited rather than trusted. A benchmark whose own
answers cannot be checked measures nothing.

It can also be pointed elsewhere with an environment variable, so anyone
can run this gate against their own questions. If the expected answers
were baked into the source that would require a fork.

## Why scheduled evaluation, not just on push

Because the failure mode has no commit attached to it.

The model gets updated underneath you. A company files an amended
report. A dependency shifts. Nothing in git moves, and the answers get
worse. Anything triggered only by pushes will never see it.

So it also runs weekly. That is four lines of cron, and it is the only
thing watching for the class of regression nobody caused.

## Why a release gate rather than monitoring

This is the one worth arguing about, and the answer is sharper than
"gates are good practice".

**You cannot monitor for this failure in production, because there is no
signal to monitor.** A hallucinated revenue figure produces a 200
response, in normal latency, with no error, in the same confident tone as
a correct one. Every dashboard stays green. Detecting that a production
answer is wrong requires knowing the right answer, and in production you
do not — that is why somebody asked.

Ground truth only exists where you have written it down in advance.
Which means the evaluation set is not a nicer version of monitoring; it
is the **only place the check can happen at all**.

So the gate is not belt-and-braces before a monitoring story. It is the
whole story, and monitoring covers what it cannot: uptime, latency,
spend, and how often answers are being withheld — that last one being a
genuinely useful production signal, since a sudden rise in refusals means
something upstream changed.

The gate has one more property monitoring never has: it acts *before*
anyone is affected. `make degraded` shows a change that passes the
linter and all 207 tests, costs sixteen right answers, and is stopped by
an exit code. Monitoring would have told us afterwards.

## Why the model runs at temperature zero

Because for a while it did not, and that quietly made the release gate
worthless.

Run twice against identical code, the evaluation scored 95.8% and then
91.7%, passing the first time and failing the second. Nothing had
changed. The model was simply sampling from the answers it considered
plausible, and on the second run one of those answers volunteered a
figure its own citation did not support.

**A gate whose verdict depends on when you asked it is not a gate.** It
would let a genuinely worse version through on a lucky run and block a
good one on an unlucky one, and once people notice that, they re-run it
until it agrees with them. At that point the check has become a
formality, which is worse than not having one, because everybody still
believes the number.

Temperature zero asks for the single most likely answer rather than a
sample. Three consecutive runs now give 46 of 48 with no unsupported
figures, every time.

There was never anything to gain from variety here. Asked what a filing
says, there is one right answer, and no reason to want it phrased
differently today than yesterday.

The wider point is the one worth keeping: **the first thing an
evaluation has to be is repeatable.** A number that moves on its own
measures the weather, and the fact that it happened to be a *good*
number twice is what made it easy to miss.

## Why a small model on purpose

If the quality came from the model, this would be an advertisement for
Anthropic rather than an engineering argument.

The whole claim is that the harness does the work, so it runs on a small
cheap one and gets 46 of 48. Swapping the model is one line of
configuration, and the gate decides whether the swap is allowed to ship —
which is the actual point being made.

## Why parsed filings are cached in memory only

A parsed filing is a cache, and the hard part of a cache is invalidation.
Persisting them to disk or Redis would mean deciding when a stored
passage has gone stale, and **a stale passage is a wrong citation** — the
one failure this project is least willing to have.

Process lifetime is a natural and correct expiry. Restart the service and
it re-reads the document.

## Why a password but not a login

The hosted demo asks for a password. It does not ask who you are, and
the difference is the point.

There are no accounts here, nothing personal is stored, and no two
visitors are treated differently — so a username would be a field to
fill in that decides nothing, plus a user table, plus password resets,
plus somewhere for personal data to end up. The only question worth
asking of this site is *may you in*, and one shared secret answers it.

It is off unless `SITE_PASSWORD` is set, so a fresh clone runs without
one. A public repository that greets you with a locked door and no key
is a repository nobody evaluates.

Four things it has to get right, none of them clever, all of them
skipped often enough to be worth listing:

- **Constant-time comparison.** `==` on strings returns as soon as two
  characters differ, and that difference is measurable across a network.
  Given enough attempts it hands the password over one character at a
  time.
- **A signed cookie.** Otherwise the cookie *is* the password and anyone
  can type `entered=yes` into a browser console. The signature covers
  the expiry too, so a visitor cannot extend their own stay by editing
  it — which is why the signature is checked before the expiry, rather
  than trusting a number the visitor chose.
- **A limit on guessing.** A password strong against a person is weak
  against a loop. Eight attempts per quarter of an hour turns "hard to
  guess" into "not worth trying". A correct password clears the count,
  because someone who mistyped twice and then remembered is not an
  attacker.
- **The API behind it too.** A gate on the page with an open `/ask`
  endpoint is decoration. The expensive part is behind the endpoint.

It is a middleware rather than a dependency on each route, because a
dependency only protects the handlers somebody remembered to decorate. A
route added next month is covered without anyone thinking about it,
which is the only kind of access control that survives a codebase
growing. `/health` and `/ready` answer regardless — an orchestrator has
no browser and no password, and a container that cannot report its own
health gets restarted forever.

**Spend is still the control that matters more.** A password decides who
gets in; it does not stop the people who are in from being expensive. So
the limits stay: a per-visitor hourly allowance and a hard daily ceiling,
the second being the one that counts, since a per-caller limit is
defeated by using more callers and only a global ceiling can promise
that tomorrow's invoice is bounded. A refused question never reaches the
model, so it costs nothing.

Both the gate and the limits live in the application rather than in the
reverse proxy. The proxy here also serves three unrelated sites, and a
control that lives in somebody else's configuration file is a control
that does not travel with the container and gets lost the first time it
is deployed anywhere else.

---

## Trade-offs made with eyes open

| Chose | Gave up | Why it was worth it |
|---|---|---|
| Keyword search | Better matching on vague questions | A ranking I can read, and every retrieval bug so far was found by reading it |
| Structure-aware chunking | Working on any document type | Citations that name the right section; the generic version got them wrong |
| Checking after generation | Paying for answers that get discarded | A check that cannot be talked round, unlike a prompt |
| Withholding answers | Some correct answers rejected | A wrongly refused answer costs a question; a fabricated one costs whoever acted on it |
| One document type | Breadth | One thing that works beats five that mostly do |
| A file for history | Concurrent writers, remote queries | No second system to operate |
| In-memory cache | Warm restarts | No invalidation logic, so no stale citations |
| 48 hand-written questions | Statistical power | Answers I verified myself, against a set I cannot quietly delete from |

The costliest of these is the fifth. This understands 10-Ks and would
need real work for an annual report filed in another jurisdiction. That
was chosen knowingly: the depth is where the interesting problems were.

---

## When each of these becomes the wrong decision

Every choice above is right at this size. Here is the specific condition
that flips each one, so that nothing here has to be defended past the
point where it stops being true.

**More than a few hundred documents, searched together.** BM25 across a
corpus is a different problem from BM25 within one document — term
statistics stop being about the document and start being about the
collection. That is where embeddings and a vector store earn their keep,
and where hybrid retrieval starts to make sense. Not before.

**Parsing on the request path becoming visible.** One second to parse a
12 MB filing is invisible when it happens once per company per process.
With thousands of documents it moves to an offline step, and *that* is
when a pipeline scheduler earns its keep — a real DAG with retries and
backfills, not Airflow installed to run one weekly cron.

**More than one replica.** The parsed-filing cache and the rate limiter
are both per-process and both would need to move — the cache behind
something shared, the limiter into Redis. Today there is one process and
a bill measured in pennies, and both would be ceremony.

**Answers that matter to someone outside the team.** The evaluation set
grows, gets versioned, and stops being one person's judgement. Somebody
other than the author has to own the expected answers, or the gate is
measuring agreement with me.

**Being asked for a number the filing does not state directly.** Nothing
here computes. It quotes. A question needing two figures divided by one
another is out of scope by construction, and adding arithmetic means
verifying arithmetic — a new and much harder problem than verifying a
quotation.
