-- Databricks notebook source
-- MAGIC %md
-- MAGIC # 05 — The report
-- MAGIC
-- MAGIC SQL only, so every cell runs on a SQL warehouse and any cell can be pinned straight
-- MAGIC onto a dashboard. No Python, no cluster.
-- MAGIC
-- MAGIC **Every table is fully qualified on purpose.** A `USE CATALOG` in a notebook cell does
-- MAGIC not travel with a query that gets lifted into a dashboard tile, and the tile fails with
-- MAGIC a table-not-found error weeks later when nobody remembers why. Qualified names work in
-- MAGIC both places.
-- MAGIC
-- MAGIC Run notebooks `00`–`04` first. This one reads, and computes nothing new.

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 1. The headline, per population, with the floor attached
-- MAGIC
-- MAGIC Three identical runs of the same prompt. `mean_correct` is the score; `floor_width` is
-- MAGIC how much that score moved on its own. **A reader who quotes the mean without the floor
-- MAGIC is quoting a number this project was built to stop.**
-- MAGIC
-- MAGIC `score_with_floor` exists so the chart label carries both numbers. A bar labelled
-- MAGIC `47.2%` invites a comparison the data cannot support; `47.2% · floor 1` does not.
-- MAGIC
-- MAGIC ### Why `run_label LIKE 'floor-a-%'` is not optional
-- MAGIC
-- MAGIC `eval_results` holds more than one prompt: `floor-a-%` is the baseline arm, `floor-b-%`
-- MAGIC is the variant from notebook `04`, and notebook `03` writes its own run as well.
-- MAGIC `floor_width` is `max - min` across the rows this query sees, so **widening the filter
-- MAGIC changes what the floor means rather than how much data it rests on.** Across one arm it
-- MAGIC is run-to-run variation with nothing changed, which is the floor. Across both arms it is
-- MAGIC variation caused by the prompt change — the quantity the floor is supposed to be
-- MAGIC compared against. Pool them and the comparison is against itself.
-- MAGIC
-- MAGIC Nothing fails when that happens. The tile renders, the label still reads `floor N`, and
-- MAGIC the number is just wrong in the direction that makes any later claim look better tested
-- MAGIC than it is.
-- MAGIC
-- MAGIC **If this query is parameterised for a dashboard filter, the filter's empty state must
-- MAGIC not mean "all runs".** A predicate of the form
-- MAGIC `(:run_label IS NULL OR array_contains(:run_label, run_label))` admits every arm as soon
-- MAGIC as nobody has chosen one, which is the state every new viewer starts in. Either default
-- MAGIC the parameter to the three `floor-a` runs, or group by `prompt_sha` as well so that two
-- MAGIC prompts can only ever appear as two rows and never as one average.

-- COMMAND ----------

WITH per_run AS (
  SELECT run_id, population,
         sum(CASE WHEN correct THEN 1 ELSE 0 END) AS correct,
         count(*)                                 AS asked
  FROM   workspace.nl2sql_eval.eval_results
  WHERE  run_label LIKE 'floor-a-%'
  GROUP  BY run_id, population
)
SELECT population,
       max(asked)                                  AS questions,
       round(100.0 * avg(correct) / max(asked), 1) AS mean_pct,
       round(avg(correct), 2)                      AS mean_correct,
       min(correct)                                AS worst_run,
       max(correct)                                AS best_run,
       max(correct) - min(correct)                 AS floor_width,
       concat(round(100.0 * avg(correct) / max(asked), 1),
              '% · floor ',
              max(correct) - min(correct))         AS score_with_floor
FROM   per_run
GROUP  BY population
ORDER  BY CASE population WHEN 'answerable'   THEN 1
                          WHEN 'unanswerable' THEN 2
                          WHEN 'ambiguous'    THEN 3 END;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 2. The finding: what happens when the data is not there
-- MAGIC
-- MAGIC Ten questions whose answer is not in the schema. Three outcomes are possible and they
-- MAGIC are not equally bad:
-- MAGIC
-- MAGIC - `correct_decline` — said it could not be answered, and named what was missing. Right.
-- MAGIC - `declined_without_reason` — said no and stopped. Unhelpful, but harmless.
-- MAGIC - `invented_answer` — **wrote confident SQL for data that does not exist.** This is the
-- MAGIC   one that reaches a stakeholder, because it comes back as a formatted number with no
-- MAGIC   error attached to it.
-- MAGIC
-- MAGIC A conventional text-to-SQL accuracy figure cannot distinguish these, because a
-- MAGIC conventional benchmark contains no question of this kind.

-- COMMAND ----------

SELECT verdict,
       count(*)                                            AS occurrences,
       count(DISTINCT question_id)                         AS distinct_questions,
       round(count(*) / count(DISTINCT run_id), 1)         AS avg_per_run
FROM   workspace.nl2sql_eval.eval_results
WHERE  run_label LIKE 'floor-a-%'
  AND  population = 'unanswerable'
GROUP  BY verdict
ORDER  BY occurrences DESC;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ### Every invented answer, with the column that was never there
-- MAGIC
-- MAGIC Read these one at a time. Counting them understates it: each row is a query that would
-- MAGIC have returned a plausible number to somebody who asked a question this database cannot
-- MAGIC answer. `what_is_missing` is the reviewer's note from the question bank, and the model
-- MAGIC never saw it.

-- COMMAND ----------

SELECT r.question_id,
       q.question_text,
       q.note      AS what_is_missing,
       r.model_sql AS what_it_wrote_instead
FROM   workspace.nl2sql_eval.eval_results r
JOIN   workspace.nl2sql_eval.questions    q USING (question_id)
WHERE  r.verdict = 'invented_answer'
  AND  r.run_label LIKE 'floor-a-%'
GROUP  BY r.question_id, q.question_text, q.note, r.model_sql
ORDER  BY r.question_id;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 3. The ambiguous questions — the quieter half of the same problem
-- MAGIC
-- MAGIC An unanswerable question has one right behaviour: decline. An ambiguous question has a
-- MAGIC harder one: **ask**. Picking a reading silently produces a defensible number that
-- MAGIC answers a different question than the one asked, and nothing anywhere reports an error.
-- MAGIC
-- MAGIC `answered_ambiguous` is that failure. It is not a wrong answer — that is exactly what
-- MAGIC makes it expensive.

-- COMMAND ----------

SELECT r.question_id,
       q.question_text,
       q.note AS why_it_is_ambiguous,
       sum(CASE WHEN r.verdict = 'correct_clarification' THEN 1 ELSE 0 END) AS times_it_asked,
       sum(CASE WHEN r.verdict = 'answered_ambiguous'    THEN 1 ELSE 0 END) AS times_it_just_picked
FROM   workspace.nl2sql_eval.eval_results r
JOIN   workspace.nl2sql_eval.questions    q USING (question_id)
WHERE  r.population = 'ambiguous'
  AND  r.run_label LIKE 'floor-a-%'
GROUP  BY r.question_id, q.question_text, q.note
ORDER  BY times_it_just_picked DESC, r.question_id;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 4. Full verdict matrix
-- MAGIC
-- MAGIC The whole result in one table, per population. Nothing is pooled into a single accuracy
-- MAGIC figure anywhere in this notebook, because answering and declining are different
-- MAGIC capabilities and averaging them hides the trade between them.

-- COMMAND ----------

SELECT population, verdict, count(*) AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY population), 1) AS pct_of_population
FROM   workspace.nl2sql_eval.eval_results
WHERE  run_label LIKE 'floor-a-%'
GROUP  BY population, verdict
ORDER  BY population, n DESC;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 5. Stability — which questions did not get the same verdict every time
-- MAGIC
-- MAGIC Identical inputs, three runs. Anything listed here is a question the system has no
-- MAGIC settled behaviour on. Note this is always at least as large as `floor_width` in section
-- MAGIC 1: two questions can flip in opposite directions and leave the total untouched, so a
-- MAGIC stable score is not a stable system.

-- COMMAND ----------

SELECT population, question_id,
       count(DISTINCT verdict)   AS distinct_verdicts,
       collect_list(verdict)     AS verdicts_across_runs
FROM   workspace.nl2sql_eval.eval_results
WHERE  run_label LIKE 'floor-a-%'
GROUP  BY population, question_id
HAVING count(DISTINCT verdict) > 1
ORDER  BY distinct_verdicts DESC, population, question_id;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 6. The A/B conclusion, as it was recorded
-- MAGIC
-- MAGIC Straight out of `eval_comparisons`, where notebook `04` wrote each delta next to the
-- MAGIC floor it was judged against. `INSIDE THE FLOOR` means this experiment cannot tell the
-- MAGIC two prompts apart — not that they are the same, and not that the change was small.

-- COMMAND ----------

SELECT population, arm_a_mean, arm_b_mean, delta, floor_width, verdict, repeats, endpoint
FROM   workspace.nl2sql_eval.eval_comparisons
ORDER  BY compared_at DESC, population;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 7. Provenance — what produced each number above
-- MAGIC
-- MAGIC `prompt_sha` covers the prompt template **and** the generated schema description
-- MAGIC together. Two runs with the same `prompt_sha` were asked the same thing; two runs with
-- MAGIC different ones were not, and their scores are not comparable regardless of how close
-- MAGIC they look.

-- COMMAND ----------

SELECT run_label, run_id, min(run_started) AS started, provider, prompt_sha,
       max(bank_size) AS bank_size, count(*) AS rows_written
FROM   workspace.nl2sql_eval.eval_results
GROUP  BY run_label, run_id, provider, prompt_sha
ORDER  BY started DESC;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC ## 8. Anything the harness itself got wrong
-- MAGIC
-- MAGIC `provider_error` means the model call failed. A gold query that failed is also recorded
-- MAGIC here rather than as a model error, so a harness fault never shows up as the model
-- MAGIC scoring badly. **Empty is the expected result; a non-empty result invalidates the
-- MAGIC affected rows above rather than lowering the score.**

-- COMMAND ----------

SELECT run_label, question_id, verdict, error
FROM   workspace.nl2sql_eval.eval_results
WHERE  verdict IN ('provider_error', 'sql_error')
ORDER  BY run_started DESC, question_id;

-- COMMAND ----------

-- MAGIC %md
-- MAGIC # What this project did NOT establish
-- MAGIC
-- MAGIC Stated here rather than in a footnote, because a results notebook that lists only its
-- MAGIC findings reads as a stronger claim than it is.
-- MAGIC
-- MAGIC - **One model, one schema, thirty questions.** Nothing here generalises to a different
-- MAGIC   model, a bigger schema, or a real agency's data model. A 30-question bank cannot
-- MAGIC   support a percentage quoted to a decimal place, which is why section 1 reports counts.
-- MAGIC - **The answerable score is a floor, not a measurement.** Grading compares one expected
-- MAGIC   result set per question, so a different correct query with a different shape — an
-- MAGIC   extra column, a different grain — grades wrong. Real answerable accuracy is at least
-- MAGIC   what section 1 says and probably higher.
-- MAGIC - **"Unanswerable" is a human judgement, not a computed property.** It rests on reading
-- MAGIC   the schema and the `note` column. Notebook `02` says so explicitly: no test
-- MAGIC   establishes it.
-- MAGIC - **A decline was checked for existing, not for being right.** The grader requires a
-- MAGIC   reason; it does not check the reason names the actual missing column. Judging that
-- MAGIC   needs a second model as judge, and a judge needs its own calibration before its
-- MAGIC   output is worth anything — which is the same argument as notebook `02`, applied one
-- MAGIC   level up.
-- MAGIC - **No cost or latency measurement.** Both belong in any real comparison and neither
-- MAGIC   was collected.
-- MAGIC - **The data is synthetic and generated by arithmetic.** It has the shape of grant
-- MAGIC   administration data and none of its messiness — no duplicate grantees, no nulls where
-- MAGIC   a real system has them, no amendments, no reporting lag. Those absences make the
-- MAGIC   questions easier than production, so treat every figure as an upper bound on
-- MAGIC   real-world behaviour.

-- COMMAND ----------

-- MAGIC %md
-- MAGIC # Dashboard
-- MAGIC
-- MAGIC Three tiles are enough, and a fourth would dilute them. Copy the query, not a
-- MAGIC screenshot — the tile re-runs against the tables and a screenshot does not.
-- MAGIC
-- MAGIC | Tile | Query | Visualisation | Why this one |
-- MAGIC |---|---|---|---|
-- MAGIC | Score with its floor | section 1 | bar, one bar per population, `floor_width` shown as a label | the score is meaningless without the floor beside it |
-- MAGIC | What happens when the data is absent | section 2 | bar, coloured by verdict | this is the finding |
-- MAGIC | Every invented answer | section 3's first query | table | the row-level evidence a reviewer will want to read |
-- MAGIC
-- MAGIC **Do not build a single "accuracy" tile.** One number pooled across the three
-- MAGIC populations is the exact artefact this project argues against: notebook `02` shows a
-- MAGIC model that declines every single question scores 18 of 30 — which would publish as
-- MAGIC "60% accurate" while answering nothing at all.
-- MAGIC
-- MAGIC **Do not parameterise the run restriction at all. Put it in the SQL, in every tile.**
-- MAGIC Measured 2026-10-01, after exactly that mistake: one `run_label` filter widget had been
-- MAGIC bound to all three datasets, and **deleting the widget did not remove the parameters.**
-- MAGIC Each query kept its `(:run_label IS NULL OR array_contains(:run_label, run_label))`
-- MAGIC predicate, which with nothing supplying a value is `NULL IS NULL` — true for every row.
-- MAGIC Two of three tiles silently widened from 3 runs to all 7.
-- MAGIC
-- MAGIC Nothing errored, and **the column that looks like a sanity check cannot see it.**
-- MAGIC `avg_per_run` in section 2 divides by `count(DISTINCT run_id)`, so it reads the same
-- MAGIC whether one arm is shown or three. Only the raw `occurrences` total moves, 30 to 70.
-- MAGIC
-- MAGIC The failure is per-tile, which is how it gets published: one tile reporting one arm
-- MAGIC beside another reporting all of them, with nothing on screen saying they differ — a
-- MAGIC dashboard breaking its own no-pooling rule in front of the reader it was built to warn.
-- MAGIC
-- MAGIC **The bar order comes from the visualisation, not the query.** An AI/BI chart sorts its
-- MAGIC categories by its own setting, so the `ORDER BY` above has no effect on the tile. Set it
-- MAGIC on the chart if the order matters.
