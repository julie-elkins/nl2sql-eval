# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Calibrate the grader, before any model is called
# MAGIC
# MAGIC **This notebook calls no model and costs no tokens.** It runs five fake models whose
# MAGIC correct scores are known in advance, and asserts all fifteen cells.
# MAGIC
# MAGIC ## Why this notebook exists
# MAGIC
# MAGIC The grader is software, and software is wrong sometimes. A wrong grader does not
# MAGIC announce itself — it produces a clean, confident, precise number that looks exactly
# MAGIC like a finding. Every figure in notebook `05` rests on this one being right, so it gets
# MAGIC checked against inputs whose answers are not in question.
# MAGIC
# MAGIC ## The contract
# MAGIC
# MAGIC Correct answers out of 12 answerable, 10 unanswerable, 8 ambiguous:
# MAGIC
# MAGIC | Fake model | answerable | unanswerable | ambiguous | What a deviation would mean |
# MAGIC |---|---|---|---|---|
# MAGIC | `gold_echo` returns the expected query | **12** | 0 | 0 | below 12: the grader fails correct answers |
# MAGIC | `always_decline` declines everything | 0 | **10** | **8** | the two halves are not graded independently |
# MAGIC | `always_wrong` valid SQL, wrong answer | 0 | 0 | 0 | any credit: the grader can be fooled |
# MAGIC | `garbage` prose, no JSON | 0 | 0 | 0 | any credit: unparseable is being read as a refusal |
# MAGIC | `decline_no_reason` bare "no" | 0 | 0 | 0 | any credit: the reason field is not required |
# MAGIC
# MAGIC The two rows that matter most are the last three all reading zero, and `gold_echo`
# MAGIC reading twelve. Together they are a floor and a ceiling: **nothing wrong scores, and
# MAGIC the right answer does.** A grader that only ever ran against a real model could be
# MAGIC missing either one and nobody would know.
# MAGIC
# MAGIC Note what `always_decline` shows: it scores 18 of 30 overall. A model that refuses
# MAGIC every question would publish as "60% accurate" on a pooled figure. That is the whole
# MAGIC argument for never pooling the populations.

# COMMAND ----------

# MAGIC %run ./_harness

# COMMAND ----------

EXPECTED = {
    "gold_echo":         {"answerable": 12, "unanswerable": 0, "ambiguous": 0},
    "always_decline":    {"answerable": 0,  "unanswerable": 10, "ambiguous": 8},
    "always_wrong":      {"answerable": 0,  "unanswerable": 0, "ambiguous": 0},
    "garbage":           {"answerable": 0,  "unanswerable": 0, "ambiguous": 0},
    "decline_no_reason": {"answerable": 0,  "unanswerable": 0, "ambiguous": 0},
}

BANK_SIZE = {"answerable": 12, "unanswerable": 10, "ambiguous": 8}

# Fail early and loudly if the bank is not what the contract above was written against.
actual_sizes = {r.population: r.n for r in spark.sql(
    "SELECT population, count(*) AS n FROM questions GROUP BY population").collect()}
assert actual_sizes == BANK_SIZE, (
    f"bank is {actual_sizes}, contract was written for {BANK_SIZE}. "
    "Update EXPECTED deliberately -- do not adjust it to match a run."
)
print(f"bank sizes match the contract: {actual_sizes}")

# COMMAND ----------

schema = schema_card()
print(f"schema description: {len(schema.splitlines())} lines, "
      f"prompt fingerprint {prompt_fingerprint(schema)}")
print()
print(schema)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run all five and check every cell

# COMMAND ----------

observed = {}
run_ids = {}

for name, fn in CALIBRATION_PROVIDERS.items():
    run_id = run_bank(f"calibration:{name}", fn, run_label=f"calibration:{name}",
                      schema=schema)
    run_ids[name] = run_id
    scores = score_by_population(run_id)
    observed[name] = {pop: scores.get(pop, (0, 0))[0] for pop in BANK_SIZE}
    print(f"{name:20s} {observed[name]}")

# COMMAND ----------

mismatches = []
for name, expected in EXPECTED.items():
    for population, want in expected.items():
        got = observed[name][population]
        if got != want:
            mismatches.append(
                f"{name} / {population}: expected {want}, got {got} "
                f"(run_id {run_ids[name]})"
            )

if mismatches:
    raise AssertionError(
        "THE GRADER IS NOT CALIBRATED. Do not run notebook 03 -- any score it produces "
        "is unverified. Failures:\n  " + "\n  ".join(mismatches)
    )

print("all 15 cells match the contract. The grader is calibrated.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The check the table above cannot make: is every `garbage` verdict actually `unparseable`?
# MAGIC
# MAGIC `garbage` scoring zero is necessary but not sufficient. Zero correct is also what a
# MAGIC grader would report if it crashed on every row and recorded `provider_error`, or if it
# MAGIC decided the prose was a refusal without a reason. Those are different bugs with the
# MAGIC same score, so the score alone cannot tell them apart — check the verdict, not the
# MAGIC total.

# COMMAND ----------

garbage_verdicts = {r.verdict: r.n for r in spark.sql(f"""
    SELECT verdict, count(*) AS n FROM {RESULTS_TABLE}
    WHERE run_id = '{run_ids["garbage"]}' GROUP BY verdict
""").collect()}

assert garbage_verdicts == {"unparseable": 30}, (
    f"expected 30 unparseable, got {garbage_verdicts}. Zero correct was reached by the "
    "wrong route."
)

bare_verdicts = {r.verdict: r.n for r in spark.sql(f"""
    SELECT verdict, count(*) AS n FROM {RESULTS_TABLE}
    WHERE run_id = '{run_ids["decline_no_reason"]}' GROUP BY verdict
""").collect()}
assert bare_verdicts == {"declined_without_reason": 30}, (
    f"expected 30 declined_without_reason, got {bare_verdicts}"
)

print(f"garbage           -> {garbage_verdicts}")
print(f"decline_no_reason -> {bare_verdicts}")
print("verdicts are reached by the right route, not just the right total.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Is the HARNESS deterministic? Run the same fake model twice.
# MAGIC
# MAGIC `gold_echo` is a fixed function, so two runs of it must produce identical verdicts.
# MAGIC Anything other than zero disagreements here is a **harness** defect, not a model one —
# MAGIC unstable row ordering in a comparison, or a query whose expected answer is not well
# MAGIC defined.
# MAGIC
# MAGIC This matters because notebook `04` measures the same quantity against a real model and
# MAGIC calls the result a noise floor. If the harness contributes noise of its own, that
# MAGIC floor is partly measuring the harness, and every comparison made against it inherits
# MAGIC the error. Establish that the floor is zero when the model cannot move, first.

# COMMAND ----------

run_a = run_bank("calibration:gold_echo", provider_gold_echo,
                 run_label="harness-determinism-a", schema=schema)
run_b = run_bank("calibration:gold_echo", provider_gold_echo,
                 run_label="harness-determinism-b", schema=schema)

flips = spark.sql(f"""
    SELECT a.question_id, a.verdict AS verdict_a, b.verdict AS verdict_b
    FROM   {RESULTS_TABLE} a
    JOIN   {RESULTS_TABLE} b USING (question_id)
    WHERE  a.run_id = '{run_a}' AND b.run_id = '{run_b}'
      AND  a.verdict <> b.verdict
""").collect()

print(f"identical inputs, two runs: {len(flips)} verdict disagreements")
if flips:
    for f in flips:
        print(f"  {f.question_id}: {f.verdict_a} -> {f.verdict_b}")
    raise AssertionError(
        "The harness is not deterministic. Fix this before measuring a noise floor in "
        "notebook 04 -- otherwise the floor includes harness noise and every comparison "
        "made against it is wrong by an unknown amount."
    )

print("harness noise floor is 0. Notebook 04's floor will measure the model only.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What this notebook did NOT check
# MAGIC
# MAGIC Stated because a calibration that only lists its passes reads as broader than it is.
# MAGIC
# MAGIC - **That the answerable questions are answerable in more than one way.** The grader
# MAGIC   compares against one expected result per question. A different correct query that
# MAGIC   returns a differently-shaped answer — an extra column, a different grain — grades
# MAGIC   as `wrong_result`. Some of what notebook `03` reports as wrong will be this.
# MAGIC - **That the unanswerable questions are truly unanswerable.** That rests on reading
# MAGIC   the `note` column and the schema by hand. No test can establish it.
# MAGIC - **That the ambiguous questions are genuinely ambiguous** rather than merely
# MAGIC   underspecified in a way one reading settles. Same: a judgement, recorded in `note`.
# MAGIC - **Whether declining is *well* done.** The grader checks a reason exists, not that
# MAGIC   the reason names the right missing column. That would need a second judge, and a
# MAGIC   judge needs its own calibration before it is worth anything.
# MAGIC - **Anything about a real model.** Every provider here is a fixed function.
