# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Measure the noise floor, then compare against it
# MAGIC
# MAGIC ## The claim this notebook exists to prevent
# MAGIC
# MAGIC *"I changed the prompt and unanswerable accuracy went from 4/10 to 6/10."*
# MAGIC
# MAGIC That sentence is unfalsifiable until you know how much 4/10 moves **when you change
# MAGIC nothing at all**. If repeat runs of the identical prompt land anywhere between 3 and 6,
# MAGIC then 4 → 6 is inside the noise and the prompt change has not been shown to do anything.
# MAGIC
# MAGIC So: run the same thing several times first, measure the spread, and only then report a
# MAGIC difference — against the spread, never on its own.
# MAGIC
# MAGIC ## Cost and stop condition, stated before starting
# MAGIC
# MAGIC `REPEATS × 30` model calls per arm, two arms. At the default of 3 that is **180 calls**
# MAGIC on a 12B model with a 600-token cap. Nothing loops, nothing retries, and every cell
# MAGIC finishes or raises — there is no path here that keeps spending while unattended. Drop
# MAGIC `REPEATS` to 2 if the workspace budget is tight; do not raise it above 5 without a
# MAGIC reason, because the floor stops widening long before the cost stops rising.

# COMMAND ----------

# MAGIC %run ./_harness

# COMMAND ----------

REPEATS = 3

ENDPOINT = find_endpoint(verbose=False)
SCHEMA_CARD = schema_card()
model_provider = make_model_provider(ENDPOINT)
print(f"system under test: {ENDPOINT}")
print(f"prompt fingerprint, arm A: {prompt_fingerprint(SCHEMA_CARD)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Arm A — the same prompt, run `REPEATS` times, nothing changed between runs
# MAGIC
# MAGIC Same endpoint, same temperature, same bank, same schema description, same prompt hash.
# MAGIC Every difference between these runs is the system's own variation.

# COMMAND ----------

arm_a_runs = []
for i in range(1, REPEATS + 1):
    run_id = run_bank(ENDPOINT, model_provider, run_label=f"floor-a-{i}", schema=SCHEMA_CARD)
    scores = score_by_population(run_id)
    arm_a_runs.append(run_id)
    print(f"run {i}  {run_id}  " + "  ".join(
        f"{p}={scores.get(p, (0, 0))[0]}/{scores.get(p, (0, 0))[1]}"
        for p in ("answerable", "unanswerable", "ambiguous")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## The floor, two ways
# MAGIC
# MAGIC **Score spread** — the range of correct answers across identical runs, per population.
# MAGIC This is the number a later comparison has to beat.
# MAGIC
# MAGIC **Verdict instability** — how many individual questions did not get the same verdict
# MAGIC every time. This is the more honest figure and it is always the larger one, because
# MAGIC two questions can flip in opposite directions and leave the total unchanged. A stable
# MAGIC total is not a stable system, and a run-to-run comparison of totals cannot see it.

# COMMAND ----------

runs_sql = ", ".join(f"'{r}'" for r in arm_a_runs)

spread = spark.sql(f"""
    WITH per_run AS (
      SELECT run_id, population, sum(CASE WHEN correct THEN 1 ELSE 0 END) AS correct
      FROM   {RESULTS_TABLE} WHERE run_id IN ({runs_sql})
      GROUP  BY run_id, population
    )
    SELECT population, min(correct) AS min_correct, max(correct) AS max_correct,
           max(correct) - min(correct) AS floor_width,
           round(avg(correct), 2) AS mean_correct
    FROM   per_run GROUP BY population ORDER BY population
""")
display(spread)

spread_rows = spread.collect()
FLOOR = {r.population: int(r.floor_width) for r in spread_rows}
BASELINE_MEAN = {r.population: float(r.mean_correct) for r in spread_rows}
print(f"score floor per population: {FLOOR}")

# COMMAND ----------

instability = spark.sql(f"""
    WITH per_question AS (
      SELECT population, question_id, count(DISTINCT verdict) AS distinct_verdicts
      FROM   {RESULTS_TABLE} WHERE run_id IN ({runs_sql})
      GROUP  BY population, question_id
    )
    SELECT population,
           count(*)                                                        AS questions,
           sum(CASE WHEN distinct_verdicts > 1 THEN 1 ELSE 0 END)          AS unstable,
           round(100.0 * sum(CASE WHEN distinct_verdicts > 1 THEN 1 ELSE 0 END) / count(*), 1) AS pct_unstable
    FROM   per_question GROUP BY population ORDER BY population
""")
display(instability)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Which questions were not stable
# MAGIC
# MAGIC Worth looking at, because the pattern tells you something the totals do not. A question
# MAGIC that alternates between `correct` and `wrong_result` is a model that is guessing. One
# MAGIC that alternates between `correct_decline` and `invented_answer` is a model with no
# MAGIC settled view on whether the data exists, which is the worse failure of the two.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT population, question_id, collect_list(verdict) AS verdicts_across_runs
# MAGIC FROM   eval_results
# MAGIC WHERE  run_label LIKE 'floor-a-%'
# MAGIC GROUP  BY population, question_id
# MAGIC HAVING count(DISTINCT verdict) > 1
# MAGIC ORDER  BY population, question_id

# COMMAND ----------

# MAGIC %md
# MAGIC ## Arm B — change exactly one thing
# MAGIC
# MAGIC One added instruction: name the columns you need, and check each one against the schema
# MAGIC before writing anything. Everything else is identical — same endpoint, same bank, same
# MAGIC temperature, same schema description.
# MAGIC
# MAGIC The prompt fingerprint below **must differ** from arm A's. If it matches, the variant is
# MAGIC not being used and the comparison is between a run and itself.

# COMMAND ----------

PROMPT_TEMPLATE_V2 = """You answer questions about a grant-awards database by writing SQL.

You may only use the tables and columns listed below. Nothing else exists.

{schema}
Before answering, do this silently:
  a. List every column you would need to answer the question.
  b. Check each one against the schema above.
  c. If any needed column is not there, the question is NOT answerable. Do not substitute
     a column that looks similar, and do not approximate.

Rules:
1. If the schema contains the data needed, return the SQL query.
2. If the schema does NOT contain the data needed, do not write a query. Say it cannot
   be answered and name what is missing.
3. If the question has more than one reasonable interpretation, do not pick one. Say it
   is ambiguous and ask the question that would settle it.
4. Read only. Never write SELECT INTO, INSERT, UPDATE, DELETE, DROP, ALTER or CREATE.

Reply with JSON and nothing else, in exactly this form:
{{"answerable": true, "sql": "SELECT ...", "reason": null}}
or
{{"answerable": false, "sql": null, "reason": "what is missing, or what to clarify"}}

Question: {question}
"""

fp_a = prompt_fingerprint(SCHEMA_CARD)
fp_b = prompt_fingerprint(SCHEMA_CARD, PROMPT_TEMPLATE_V2)
assert fp_a != fp_b, "the two arms hash identically -- arm B is not using the variant"
print(f"arm A {fp_a}\narm B {fp_b}")

# COMMAND ----------

arm_b_runs = []
for i in range(1, REPEATS + 1):
    run_id = run_bank(ENDPOINT, model_provider, run_label=f"floor-b-{i}",
                      schema=SCHEMA_CARD, template=PROMPT_TEMPLATE_V2)
    scores = score_by_population(run_id)
    arm_b_runs.append(run_id)
    print(f"run {i}  {run_id}  " + "  ".join(
        f"{p}={scores.get(p, (0, 0))[0]}/{scores.get(p, (0, 0))[1]}"
        for p in ("answerable", "unanswerable", "ambiguous")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## The comparison, reported against the floor
# MAGIC
# MAGIC The rule, fixed before the numbers were seen: **a change smaller than the floor width
# MAGIC is reported as not measurable, not as a small improvement.** Choosing that rule after
# MAGIC looking at the result is how a noise floor becomes decoration.
# MAGIC
# MAGIC "Not measurable" does not mean the change did nothing. It means this experiment, at this
# MAGIC many repeats, cannot tell. The honest options then are more repeats or a bigger bank —
# MAGIC not a softer claim.

# COMMAND ----------

b_runs_sql = ", ".join(f"'{r}'" for r in arm_b_runs)

b_spread = spark.sql(f"""
    WITH per_run AS (
      SELECT run_id, population, sum(CASE WHEN correct THEN 1 ELSE 0 END) AS correct
      FROM   {RESULTS_TABLE} WHERE run_id IN ({b_runs_sql})
      GROUP  BY run_id, population
    )
    SELECT population, min(correct) AS min_correct, max(correct) AS max_correct,
           max(correct) - min(correct) AS floor_width, round(avg(correct), 2) AS mean_correct
    FROM   per_run GROUP BY population ORDER BY population
""").collect()

B_MEAN = {r.population: float(r.mean_correct) for r in b_spread}
B_FLOOR = {r.population: int(r.floor_width) for r in b_spread}

print(f"{'population':14s} {'A mean':>7s} {'B mean':>7s} {'delta':>7s} "
      f"{'floor':>6s}  verdict")
print("-" * 72)

verdict_rows = []
for population in ("answerable", "unanswerable", "ambiguous"):
    a, b = BASELINE_MEAN[population], B_MEAN[population]
    delta = b - a
    # The wider of the two arms' floors. Using only arm A's would understate the
    # variation the comparison actually has to clear.
    floor = max(FLOOR[population], B_FLOOR[population])
    if abs(delta) <= floor:
        verdict = f"INSIDE THE FLOOR ({floor}) -- NOT MEASURABLE"
    else:
        verdict = f"moved beyond the floor ({floor})"
    verdict_rows.append((population, a, b, delta, floor, verdict))
    print(f"{population:14s} {a:>7.2f} {b:>7.2f} {delta:>+7.2f} {floor:>6d}  {verdict}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write the comparison down, so the claim and its floor travel together
# MAGIC
# MAGIC A delta stored without the floor it was judged against becomes a bare number in a
# MAGIC fortnight, and a bare number gets quoted.

# COMMAND ----------

from pyspark.sql import Row

spark.sql("""
CREATE TABLE IF NOT EXISTS eval_comparisons (
  compared_at   TIMESTAMP,
  endpoint      STRING,
  repeats       INT     COMMENT 'Runs per arm. The floor is only as good as this number.',
  arm_a_prompt  STRING  COMMENT 'Prompt fingerprint for arm A.',
  arm_b_prompt  STRING  COMMENT 'Prompt fingerprint for arm B. Differs from A by construction.',
  population    STRING,
  arm_a_mean    DOUBLE,
  arm_b_mean    DOUBLE,
  delta         DOUBLE,
  floor_width   INT     COMMENT 'Measured spread across identical runs, wider of the two arms.',
  verdict       STRING  COMMENT 'A delta at or inside floor_width is NOT MEASURABLE, never a small gain.'
) COMMENT 'Every A/B conclusion, stored with the noise floor it was judged against.'
""")

import datetime as _dt

spark.createDataFrame(
    [Row(compared_at=_dt.datetime.now(), endpoint=ENDPOINT, repeats=REPEATS,
         arm_a_prompt=fp_a, arm_b_prompt=fp_b, population=p,
         arm_a_mean=float(a), arm_b_mean=float(b), delta=float(d),
         floor_width=int(f), verdict=v)
     for p, a, b, d, f, v in verdict_rows],
    schema=spark.table("eval_comparisons").schema,
).write.mode("append").insertInto("eval_comparisons")

display(spark.table("eval_comparisons").orderBy("compared_at", "population"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## What this notebook did NOT establish
# MAGIC
# MAGIC - **A floor from 3 runs is a rough floor.** Three runs can easily miss the widest
# MAGIC   spread, so the real variation is at least this and probably more. It is a lower
# MAGIC   bound, and treating a lower bound as the bound favours finding an effect.
# MAGIC - **Nothing here is a statistical test.** No confidence interval, no p-value, 30
# MAGIC   questions. "Beyond the floor" means larger than the variation actually observed,
# MAGIC   which is a weaker claim than significance and should not be reported as one.
# MAGIC - **One endpoint, one temperature, one bank.** Nothing generalises to another model.
# MAGIC - **Cost was not measured.** Arm B's prompt is longer, so if the arms scored the same
# MAGIC   the longer one is worse. That comparison needs token accounting nobody did here.
# MAGIC - **A floor of zero would not prove determinism.** It would mean three runs failed to
# MAGIC   find variation, at a temperature that only reduces it.
