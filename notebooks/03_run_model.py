# Databricks notebook source
# MAGIC %md
# MAGIC # 03 — Run a real model against the bank
# MAGIC
# MAGIC The first notebook that spends anything. `%run ./_harness` pulls in the same grader
# MAGIC notebook `02` calibrated, so **run `02` first**. A score from an uncalibrated grader is
# MAGIC not a weaker result, it is not a result.

# COMMAND ----------

# MAGIC %run ./_harness

# COMMAND ----------

# MAGIC %md
# MAGIC ## Find an endpoint that works, rather than assuming one

# COMMAND ----------

ENDPOINT = find_endpoint()
print(f"\nsystem under test: {ENDPOINT}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Try one question before spending thirty
# MAGIC
# MAGIC `modelParameters` is not accepted by every endpoint. Find that out on one call, not on
# MAGIC the twenty-ninth. If the probe fails mentioning `modelParameters`, rebuild the provider
# MAGIC with `with_parameters=False` and try again.

# COMMAND ----------

SCHEMA_CARD = schema_card()
model_provider = make_model_provider(ENDPOINT)

probe_question = spark.table("questions").where("question_id = 'A02'").collect()[0]
probe = model_provider(probe_question, build_prompt(probe_question.question_text, SCHEMA_CARD))

print(f"parse_ok       : {probe.parse_ok}")
print(f"provider_error : {probe.provider_error}")
print(f"answerable     : {probe.answerable}")
print(f"sql            : {probe.sql}")
print(f"\nraw reply:\n{probe.raw[:1000]}")

if probe.provider_error and "modelParameters" in probe.provider_error:
    print("\nRetrying without modelParameters...")
    model_provider = make_model_provider(ENDPOINT, with_parameters=False)
    probe = model_provider(probe_question,
                           build_prompt(probe_question.question_text, SCHEMA_CARD))
    print(f"parse_ok: {probe.parse_ok}  provider_error: {probe.provider_error}")

if probe.provider_error:
    raise RuntimeError(f"The probe call failed and the bank was not run: {probe.provider_error}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the bank
# MAGIC
# MAGIC Thirty questions, one call each, appended to `eval_results` with the prompt
# MAGIC fingerprint, the endpoint and the bank size recorded on every row.

# COMMAND ----------

baseline_run = run_bank(ENDPOINT, model_provider, run_label="baseline", schema=SCHEMA_CARD)
print(f"run_id {baseline_run}\n")

for population in ("answerable", "unanswerable", "ambiguous"):
    correct, total = score_by_population(baseline_run).get(population, (0, 0))
    pct = f"{100 * correct / total:.0f}%" if total else "n/a"
    print(f"{population:14s} {correct:>2d} / {total:<2d}  {pct}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read the verdicts, not the totals
# MAGIC
# MAGIC Three percentages are still a summary. The finding lives in the verdict breakdown:
# MAGIC `invented_answer` counts questions where the model produced confident SQL for data that
# MAGIC does not exist.
# MAGIC
# MAGIC **Do not quote any of these numbers yet.** Notebook `04` measures how much of this
# MAGIC moves when nothing changes. Until that floor exists there is no way to tell a real
# MAGIC difference from run-to-run variation.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT population, verdict, count(*) AS n
# MAGIC FROM   eval_results
# MAGIC WHERE  run_id = (SELECT max_by(run_id, run_started) FROM eval_results WHERE run_label = 'baseline')
# MAGIC GROUP  BY population, verdict
# MAGIC ORDER  BY population, n DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ### The questions where it invented an answer
# MAGIC
# MAGIC Worth reading one at a time rather than counting. Each row is a query that would have
# MAGIC handed a number to somebody who asked a question this data cannot answer — and
# MAGIC `what_is_missing` is the column that was never there.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT r.question_id, q.question_text, q.note AS what_is_missing, r.model_sql
# MAGIC FROM   eval_results r
# MAGIC JOIN   questions q USING (question_id)
# MAGIC WHERE  r.verdict = 'invented_answer'
# MAGIC   AND  r.run_id = (SELECT max_by(run_id, run_started) FROM eval_results WHERE run_label = 'baseline')
# MAGIC ORDER  BY r.question_id

# COMMAND ----------

# MAGIC %md
# MAGIC ### Any query the read-only check refused
# MAGIC
# MAGIC Empty is the expected result and is not a finding either way. A non-empty result is
# MAGIC worth reading closely: the prompt asked for read-only SQL, so anything here is a case
# MAGIC where asking politely was not enough — which is the argument for the check existing.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT question_id, error, model_sql
# MAGIC FROM   eval_results
# MAGIC WHERE  error LIKE 'refused as not read-only%'
# MAGIC ORDER  BY run_started DESC, question_id
