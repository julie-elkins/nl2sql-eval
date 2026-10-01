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

![Three-tile dashboard. Top tile, "Score, with the floor it moved on its own": percent correct
per question type, averaged over three runs of an identical prompt, with each bar's floor
welded into its axis label — "answerable · floor 1" at 47.2, "ambiguous · floor 0" at 62.5,
"unanswerable · floor 0" at 90.0. Its note adds that populations are never pooled, because a
model declining all 30 questions would score 60% on a blended figure. Middle tile, "Verdict
breakdown (unanswerable)": 27 correct declines against 3 invented answers, out of 10
questions over 3 runs. Bottom tile, "Invented answers": a single row, question U07, whose
missing data is noted as fiscal years ending at 2025, and whose generated query reads SELECT
SUM(authorized_amount) FROM programs WHERE fiscal_year = 2027.](docs/dashboard.png)

*Every score is reported with the floor beside it — how many answers changed across three
runs of an identical prompt. A difference smaller than the floor has not been measured. The
floor is part of each bar's category name rather than a chart label, because a label is a
display setting and can be switched off by someone editing something else.*

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

The answerable floor of 1 is a single question, `A01`, and the mechanism is worth seeing. All
three runs produced structurally identical SQL — same three-table join, same `GROUP BY`, same
aggregate. The run that failed had aliased `programs` as `B` and `awards` as `C`, then selected
`SUM(B.award_amount)`, a column that lives on `awards`. The query did not run.

**Nothing about the model's reading of the question moved between runs. Its bookkeeping moved.**
That is the whole case for measuring a floor before reporting a delta: a prompt change that
shifts the score by one answer has not been shown to change anything the model understands.

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

### The mirror image: `A07` refused a question it could have answered

Over-claiming and over-refusing are opposite failures with opposite fixes, which is why the
verdict vocabulary keeps `invented_answer` and `wrongly_declined` apart rather than pooling
them into "wrong". **This run produced a reproducible instance of each.** `U07` invented an
answer three times out of three. `A07` — *"how many awards have not been paid out in full?"* —
declined three times out of three, and it is answerable: sum each award's disbursements and
compare the total to the award amount.

The likely cause is one of the two deliberate traps in the schema comments. `disbursements`
carries a comment saying the payments are not required to sum to the award amount, written to
stop a model assuming they do. **It appears to have persuaded the model the question could not
be answered at all.** The recorded `model_reason` for each decline would settle that and has
not been read.

A caveat in a schema description changing behaviour in a direction nobody designed for is the
kind of thing worth knowing before deployment, and it is only visible because refusals are
graded per population. One pooled number cannot show a model over-claiming and over-refusing
at the same time.

### On the ambiguous half

Five of eight ambiguous questions drew a request for clarification instead of an answer. The
three that did not are the more expensive failure mode: an answer that is not wrong, merely
answering a different question than the one asked, with nothing in the output to say so.

### Why the answerable figure is 47.2%, cause by cause

47.2% is 17 correct out of 12 questions over 3 runs. The obvious guess is that the grader is
too strict, and **that guess is wrong**: of the 19 failures, three questions never reached
result comparison at all, so no amount of leniency could have changed them.

| Cause | Occurrences | Questions | Grading artefact? |
|---|---|---|---|
| Query written in the wrong SQL dialect | 3 | `A09` | no |
| Table alias bound to the wrong table | 1 | `A01` | no |
| Refused a question that was answerable | 3 | `A07` | no |
| Reply carried no `answerable` boolean | 3 | `A06` | no |
| Listed the rows instead of counting them | 3 | `A12` | no |
| Omitted a column the expected query returned | 6 | `A03`, `A11` | **yes** |

**Thirteen of nineteen failures are the model. Six are the grader** — and both of those are
the same defect, which is not in the grader:

- `A03` — *"which five grantees have received the most money in total?"* The model returned the
  five grantee names, correctly ranked. The expected query also returns the total, so the
  tuples differ and it grades wrong.
- `A11` — *"what is the single largest award, and which grantee and program is it under?"* The
  model returned the amount, the grantee and the program. The expected query also returns
  `award_id`, a surrogate key the question never asks for.

So **the fault is in the question bank.** `results_match` is doing exactly what it was written
to do; it was handed two gold queries that ask for more than the question does. That is a
one-line fix per question and a 30-call re-run, and it has not been done — so this README
reports the band rather than the flattering end of it.

**Credit those two questions and the answerable figure is 63.9% rather than 47.2%.** Neither
number is the accuracy. 47.2% is what was measured. 63.9% is what you get by hand-crediting
two questions after watching them fail, which is the move a calibrated grader exists to
prevent. The true figure is in that band, and the only honest way to close it is to fix the
bank and re-run.

`A09` is the most actionable of the genuine failures: the model wrote
`strftime('%Y', disbursement_date)`, a SQLite function Databricks does not have, on all three
runs. **The prompt never names the SQL dialect.** That is a one-line prompt change and the
obvious next A/B arm — the floor is 1, so recovering those three answers would clear it.

The two non-answerable figures compare nothing against a gold result set, so none of this
touches them.

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

## Built on Databricks

Everything runs inside the workspace. No local Python, no external API key, and no data
leaves Unity Catalog.

| Used for | How |
|---|---|
| Data and results | Five Delta tables in `workspace.nl2sql_eval`, plus `eval_results` and `eval_comparisons` |
| The schema the model is shown | Read from `information_schema.columns` at run time, so the prompt cannot drift away from the tables |
| The system under test | `ai_query()` against a serving endpoint, with `modelParameters => named_struct('temperature', …)` to pin sampling |
| The report | Notebook `05` is SQL only, so every cell runs on a SQL warehouse and any cell pins straight to a dashboard |
| The tiles | An AI/BI dashboard, three tiles, each one a query from notebook `05` |

Three platform details that changed the design rather than decorating it:

- **`insertInto`, not `saveAsTable`.** `saveAsTable` replaces the table *definition*, which
  silently drops the 23 column comments notebook `00` sets — and those comments **are** the
  prompt. The eval would carry on running, scoring, and reporting, while quietly no longer
  telling the model what any column means.
- **Parameter markers, not f-strings.** Model output reaches `ai_query` through
  `spark.sql(query, args={...})`, so a reply containing a quote character cannot alter the
  statement around it.
- **No RDD access anywhere.** Serverless compute runs Spark Connect, where `df.rdd` and
  `spark.sparkContext` raise rather than degrade. The harness is DataFrame and SQL throughout.

Built on **Free Edition**, so it reproduces at no infrastructure cost.

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
- **The answerable score is a lower bound, now with a measured ceiling.** Two of the twelve
  gold queries return a column the question does not ask for, so 47.2% is the floor and 63.9%
  the ceiling. Neither end has been confirmed by a re-run against a corrected bank.
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

Notebook `03` discovers an available model endpoint at run time rather than hard-coding one,
because the endpoint list differs between workspaces.
