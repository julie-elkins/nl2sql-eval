# nl2sql-eval — evaluating a text-to-SQL assistant on the questions it should refuse

A six-notebook evaluation harness that runs entirely inside Databricks: Unity Catalog for
the data and the results, a foundation model endpoint as the system under test, and a SQL
dashboard for the report.

**The deliverable is the evaluation, not the demo.** Getting a language model to write SQL
against a small schema is a weekend exercise and the result is nearly always impressive.
This project measures the part that decides whether such a system is safe to put in front
of a programme manager: **what it does when the answer is not in the data.**

---

## The problem

Published text-to-SQL accuracy figures are computed on question sets where every question
is answerable. That measurement cannot see the failure that matters in production.

Ask a grant-management database *"how many employees does each grantee have?"* when it holds
no headcount column, and a fluent model will usually write a query anyway — joining
something plausible, aggregating something adjacent, returning a formatted number. No error
is raised. Nothing in the result indicates that the question was unanswerable. Somebody puts
the number in a briefing.

A system that scores 95% on answerable questions and 0% on unanswerable ones is not a 95%
system. It is a system that fabricates a figure whenever it is asked something outside its
schema — which, in real use, is most of the time.

**What this one did:** declined nine of ten out-of-schema questions correctly, and fabricated
the tenth on every single run — a reproducible blind spot around missing *time* rather than
missing columns. See [What it found](#what-it-found).

![Bar chart of percent correct per question type across three identical runs: ambiguous 62.5%
with a floor of 0, answerable 47.2% with a floor of 1, unanswerable 90.0% with a floor of 0.
Each bar is labelled with its score and its floor.](docs/dashboard-score-with-floor.png)

*Every score is reported with the floor beside it — how many answers changed across three
runs of an identical prompt. A difference smaller than the floor has not been measured.*

## What this measures

Thirty questions in three populations, **graded separately and never pooled**:

| Population | n | Correct behaviour |
|---|---|---|
| `answerable` | 12 | return SQL whose result matches the expected answer |
| `unanswerable` | 10 | **decline**, and name what is missing |
| `ambiguous` | 8 | **ask**, because the question has more than one honest reading |

The ambiguous population is the interesting one. An unanswerable question has a clean right
behaviour. An ambiguous one — *"show me funding for the Northeast"*, against a schema where
`agencies.region` is the **agency's** region and `grantees.state` is where the money went —
supports two defensible answers that return different totals. Picking one silently is how a
stakeholder ends up with a number that does not mean what they think it means, and nobody
sees an error, because there isn't one.

**Why the populations are never averaged:** a model that declines all thirty questions scores
18 of 30. On a pooled figure that publishes as *"60% accurate"* while answering nothing at
all. Notebook `02` asserts that number deliberately, as the argument.

## What it found

Three runs of an identical prompt against the foundation-model endpoint this workspace
serves. Notebook `03` discovers that endpoint at run time and records it, along with the
prompt fingerprint, on every row of `eval_results` — section 7 of notebook `05` prints them.

| Population | n | Mean correct | Floor |
|---|---|---|---|
| `answerable` | 12 | 47.2% | 1 |
| `unanswerable` | 10 | **90.0%** | 0 |
| `ambiguous` | 8 | 62.5% | 0 |

**Floor** is how many answers changed across the three identical runs. A later comparison has
to beat that number before it means anything.

### The finding: one reproducible blind spot, not general fabrication

Nine of the ten unanswerable questions were declined correctly, every run. The model is
mostly good at noticing that data is absent — which is a more interesting result than
wholesale hallucination, because it makes the one failure diagnostic rather than typical.

**`U07` was fabricated on all three runs.** Asked *"what is the projected award spend for
fiscal year 2027?"* against data that ends at fiscal 2025 and holds no forecast column, it
answered:

```sql
SELECT SUM(authorized_amount) FROM programs WHERE fiscal_year = 2027
```

That is confident, syntactically valid, semantically empty SQL. It returns nothing, or it
returns a number that is not a projection, and either way it comes back formatted with no
error attached.

The pattern is specific: the model recognised nine kinds of missing **column** and did not
recognise missing **future time**. `fiscal_year` exists, `2027` is a plausible value for it,
and that was apparently enough. A floor of 0 means this is not a sampling fluke — it is
reproducible, which makes it the kind of failure you could actually build a guard against.

A benchmark containing only answerable questions cannot surface this at all.

### On the ambiguous half

Five of eight ambiguous questions drew a request for clarification instead of an answer. The
three that did not are the more expensive failure mode: an answer that is not wrong, merely
answering a different question than the one asked, with nothing in the output to say so.

### The answerable figure is not yet trustworthy, and that is the next thing to fix

47.2% is roughly 5.7 of 12, on ordinary aggregation queries — group by agency, count by
status, sum by fiscal year. A model failing half of those is possible but it is not the most
likely explanation.

The likelier one is a limitation this project already documents: grading compares against one
expected result set, so a correct query returning a differently-shaped answer — an extra
column, a different grain, a decimal that rounds differently — is scored `wrong_result`. Until
the failures are separated into genuine errors and grading artefacts, **this number should be
read as a lower bound on the model and an upper bound on the grader's leniency, and not
quoted as accuracy.**

The two non-answerable figures do not depend on result-shape comparison at all, so they are
unaffected.

## Two things that make the result trustworthy

These are the parts that took the work, and they are the parts a reviewer should look at.

**1. The grader is calibrated before any model is called.** Notebook `02` runs five fake
models whose correct scores are known in advance — one that returns the expected query, one
that declines everything, one that returns valid SQL with a wrong answer, one that returns
prose instead of JSON, one that declines with no reason — and asserts all fifteen cells. It
costs nothing and calls nothing.

The floor and the ceiling are both needed. A grader that only ever ran against a real model
could be silently failing correct answers, or silently crediting garbage, and the score would
look identical either way: clean, confident, precise, and wrong. It also checks *which*
verdict each fake model earned, not just its total — zero correct can be reached by
crediting nothing or by crashing on every row, and the score cannot tell those apart.

**2. Nothing is reported as a result until the noise floor is measured.** Notebook `04` runs
the identical prompt three times and records how much the score moves when nothing changes.
Only then does it compare a prompt variant against that floor.

A delta smaller than the floor is reported as **`INSIDE THE FLOOR — NOT MEASURABLE`**, never
as a small improvement. The rule is fixed before the numbers are seen, because choosing it
afterwards is how a noise floor becomes decoration. Every comparison is written to
`eval_comparisons` **with its floor stored alongside it**, so the claim and the thing that
qualifies it cannot get separated later.

## Grading

- **Execution match.** Queries are compared on the rows they return, never on their text.
  There are many correct ways to write the same query.
- **Row order is ignored** except on questions where ordering is part of the answer, and
  every one of those carries a tie-break column — otherwise two rows with equal totals come
  back in either order and the grader fails a correct query at random. A flaky eval is worse
  than no eval, because the flakiness gets attributed to the model.
- **No expected answer is allowed to be empty.** Result-set comparison scores a wrong query
  returning nothing as correct if the expected answer is also nothing — `SELECT … WHERE 1=0`
  would pass. Notebook `01` raises if any expected answer comes back empty.
- **Gating is on a structured field, never on prose.** The model must return
  `{"answerable": bool, …}`. A reply with no boolean is `unparseable`, which is never counted
  as a refusal — inferring "it declined" from prose would credit a model for being vague.
- **A read-only check runs before any generated SQL is executed**, independently of the
  prompt asking for read-only SQL. Asking is not a control.
- **Harness failures are recorded as `provider_error`, not as the model scoring badly.**

## The schema

Five tables of synthetic public-sector grant data — agencies, programs, grantees, awards,
disbursements — generated by index arithmetic, with column comments that are load-bearing:
the model's prompt is built from `information_schema.columns`, so the schema description
cannot drift away from the tables.

What is deliberately **absent** is the point: no headcount, no performance measures, no
applications or denials, no appropriation source, no audit findings, no approval timestamps,
no forecasts, no population figures. Each absence is a question in the bank.

Two comments are deliberate traps, and both are true: `agencies.region` describes the agency
rather than the grantee, and disbursements are not required to sum to the award amount.

## Running it

Notebooks run in order. `00` and `01` are idempotent; `02` costs nothing.

| Notebook | What it does | Cost |
|---|---|---|
| `00_setup_and_generate` | creates the schema and five Delta tables | none |
| `01_question_bank` | writes the 30 questions, validates the bank | none |
| `_harness` | shared definitions, `%run` by 02–04 | — |
| `02_calibrate_grader` | five fake models, fifteen assertions | **none — calls no model** |
| `03_run_model` | one baseline run against a real endpoint | 30 calls |
| `04_noise_floor` | 3 runs × 2 prompt arms, floor and comparison | 180 calls |
| `05_report_and_dashboard` | SQL only, runs on a warehouse | none |

`_harness` is `%run` by `02`, `03` and `04`, so **the grader that was calibrated is
byte-for-byte the grader that scores.** A calibration run against a copy of the grader
proves nothing about the one that produced the numbers.

Notebook `04` states its cost and its stop condition before it starts. Nothing here loops or
retries; every cell finishes or raises.

## What this does NOT establish

- **One model, one schema, thirty questions.** Nothing generalises to a different model or a
  real agency's data model. Thirty questions cannot support a percentage quoted to a decimal
  place, so the report gives counts.
- **The answerable score is a lower bound.** One expected result set per question means a
  differently-shaped correct answer grades wrong.
- **"Unanswerable" is a human judgement**, recorded in the bank's `note` column. No test
  establishes it, and notebook `02` says so.
- **A decline was checked for existing, not for being right.** The grader requires a reason;
  it does not verify the reason names the actual missing column. That needs a model as judge
  — and a judge needs its own calibration first, which is the same argument as notebook `02`
  one level up.
- **A three-run floor is a lower bound on the variation**, and treating a lower bound as the
  bound favours finding an effect.
- **"Beyond the floor" is not statistical significance.** No confidence interval, no p-value.
  It means larger than the variation actually observed, which is a weaker claim.
- **No cost or latency was measured.** The variant prompt is longer, so equal scores would
  make it the worse option — and that comparison needs token accounting nobody did here.
- **The data is synthetic and tidy.** No duplicate grantees, no nulls, no amendments, no
  reporting lag. Those absences make the questions easier than production, so every figure
  is an upper bound on real-world behaviour.

## Notes

All data is synthetic, generated by arithmetic in notebook `00`. Contact addresses use the
RFC 2606 `.invalid` reserved domain. Nothing here derives from any real organisation's data.

Built on Databricks Free Edition. Catalog and schema names are `workspace.nl2sql_eval`;
notebook `03` discovers an available model endpoint at run time rather than hard-coding one,
because the endpoint list differs between workspaces.
