# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — The question bank
# MAGIC
# MAGIC Thirty questions in three populations. The bank is the contribution of this project;
# MAGIC the SQL generation is the easy part.
# MAGIC
# MAGIC | Population | Count | A correct system… |
# MAGIC |---|---|---|
# MAGIC | `answerable` | 12 | returns SQL whose result matches the expected answer |
# MAGIC | `unanswerable` | 10 | **declines**, because the schema does not hold the data |
# MAGIC | `ambiguous` | 8 | **asks**, because the question has more than one honest reading |
# MAGIC
# MAGIC ## Why the two non-answerable populations exist
# MAGIC
# MAGIC Almost every published text-to-SQL accuracy figure is computed on a set where every
# MAGIC question is answerable. Such a set cannot see the failure that actually matters in
# MAGIC production: a confident query for data that does not exist. The result comes back
# MAGIC formatted, numeric and plausible, and somebody puts it in a briefing.
# MAGIC
# MAGIC A system that scores 95% on answerable questions and 0% on unanswerable ones is not
# MAGIC a 95% system. It is a system that will fabricate a figure whenever it is asked
# MAGIC something outside the schema, which in real use is most of the time.
# MAGIC
# MAGIC ## The two populations are never pooled into one number
# MAGIC
# MAGIC Answering and declining are different capabilities with different costs, and averaging
# MAGIC them hides the trade: a model tuned to decline more scores better on one half and
# MAGIC worse on the other, and a single blended accuracy figure moves hardly at all. Every
# MAGIC report in notebook `05` is **per population**.

# COMMAND ----------

CATALOG, SCHEMA = "workspace", "nl2sql_eval"
spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Answerable — 12 questions with an expected answer
# MAGIC
# MAGIC Two deliberate choices in the expected SQL:
# MAGIC
# MAGIC **Every `ORDER BY … LIMIT` has a tie-break column.** Without one the expected answer
# MAGIC is not well defined — two rows with equal totals can come back in either order and the
# MAGIC grader would fail a correct query at random. An eval that is flaky is worse than no
# MAGIC eval, because the flakiness gets attributed to the model.
# MAGIC
# MAGIC **No expected answer is allowed to be empty.** See notebook `02` for why: a grader
# MAGIC that compares result sets scores "wrong query returning nothing" as correct when the
# MAGIC expected answer is also nothing, so an empty expected answer is a hole in the grading
# MAGIC rather than a hard question. The last cell here enforces it.

# COMMAND ----------

ANSWERABLE = [
    ("A01", "What is the total dollar amount awarded by each agency?", False, """
        SELECT ag.agency_name, SUM(aw.award_amount) AS total_awarded
        FROM   awards aw
        JOIN   programs p  ON aw.program_id = p.program_id
        JOIN   agencies ag ON p.agency_id   = ag.agency_id
        GROUP BY ag.agency_name
        ORDER BY ag.agency_name
    """),
    ("A02", "How many awards are there in each status?", False, """
        SELECT status, count(*) AS award_count
        FROM   awards
        GROUP BY status
        ORDER BY status
    """),
    ("A03", "Which five grantees have received the most money in total?", True, """
        SELECT g.grantee_name, SUM(aw.award_amount) AS total_awarded
        FROM   awards aw
        JOIN   grantees g ON aw.grantee_id = g.grantee_id
        GROUP BY g.grantee_name
        ORDER BY total_awarded DESC, g.grantee_name
        LIMIT 5
    """),
    ("A04", "How many awards were made under fiscal year 2024 programs, and what do they total?", False, """
        SELECT count(*) AS award_count, SUM(aw.award_amount) AS total_awarded
        FROM   awards aw
        JOIN   programs p ON aw.program_id = p.program_id
        WHERE  p.fiscal_year = 2024
    """),
    ("A05", "What is the average award amount for each type of grantee?", False, """
        SELECT g.grantee_type, AVG(aw.award_amount) AS avg_award
        FROM   awards aw
        JOIN   grantees g ON aw.grantee_id = g.grantee_id
        GROUP BY g.grantee_type
        ORDER BY g.grantee_type
    """),
    ("A06", "Which programs have awarded more than they were authorised to award?", False, """
        SELECT p.program_name, p.authorized_amount, SUM(aw.award_amount) AS total_awarded
        FROM   programs p
        JOIN   awards aw ON aw.program_id = p.program_id
        GROUP BY p.program_name, p.authorized_amount
        HAVING SUM(aw.award_amount) > p.authorized_amount
        ORDER BY p.program_name
    """),
    ("A07", "How many awards have not been paid out in full?", False, """
        SELECT count(*) AS not_fully_disbursed
        FROM ( SELECT aw.award_id
               FROM   awards aw
               JOIN   disbursements d ON d.award_id = aw.award_id
               GROUP BY aw.award_id, aw.award_amount
               HAVING SUM(d.amount) < aw.award_amount )
    """),
    ("A08", "How many grantees are there in each state?", False, """
        SELECT state, count(DISTINCT grantee_id) AS grantees
        FROM   grantees
        GROUP BY state
        ORDER BY state
    """),
    ("A09", "How much was paid out in calendar year 2024?", False, """
        SELECT SUM(amount) AS disbursed_2024
        FROM   disbursements
        WHERE  year(disbursement_date) = 2024
    """),
    ("A10", "How many programs does each agency run in each fiscal year?", False, """
        SELECT ag.agency_name, p.fiscal_year, count(*) AS programs
        FROM   programs p
        JOIN   agencies ag ON p.agency_id = ag.agency_id
        GROUP BY ag.agency_name, p.fiscal_year
        ORDER BY ag.agency_name, p.fiscal_year
    """),
    ("A11", "What is the single largest award, and which grantee and program is it under?", True, """
        SELECT aw.award_id, aw.award_amount, g.grantee_name, p.program_name
        FROM   awards aw
        JOIN   grantees g ON aw.grantee_id = g.grantee_id
        JOIN   programs p ON aw.program_id = p.program_id
        ORDER BY aw.award_amount DESC, aw.award_id
        LIMIT 1
    """),
    ("A12", "How many grantees hold awards under more than one program?", False, """
        SELECT count(*) AS grantees_in_multiple_programs
        FROM ( SELECT grantee_id
               FROM   awards
               GROUP BY grantee_id
               HAVING count(DISTINCT program_id) > 1 )
    """),
]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Unanswerable — 10 questions the schema cannot support
# MAGIC
# MAGIC Every one is a question a real programme manager would ask, and every one needs a
# MAGIC column, a table or a timestamp this schema does not have. None is a trick: the model
# MAGIC is given the full schema with comments, so declining requires only reading what it
# MAGIC was handed.
# MAGIC
# MAGIC The `note` column records *what is missing*. **It is never shown to the model** —
# MAGIC it is there so a reviewer can check the question is genuinely unanswerable rather
# MAGIC than merely hard, and so a disputed verdict can be settled by reading the bank.

# COMMAND ----------

UNANSWERABLE = [
    ("U01", "How many employees does each grantee have?",
     "No headcount or staffing column anywhere."),
    ("U02", "Which programs met their performance targets?",
     "No outcome, target or performance measure exists."),
    ("U03", "How many applications were denied for each program?",
     "Only awards are recorded. There is no applications table, so denials cannot exist."),
    ("U04", "Which appropriation act funded the Grid Modernization program?",
     "No appropriation or funding-source column."),
    ("U05", "What did the auditors find on the suspended awards?",
     "Status records that an award is Suspended. No audit finding is stored."),
    ("U06", "On average, how many days pass between application and award approval?",
     "Only award_date exists. There is no application or approval timestamp."),
    ("U07", "What is the projected award spend for fiscal year 2027?",
     "Data ends at fiscal 2025 and there is no forecast, plan or projection column."),
    ("U08", "What is the per-capita grant funding for each state?",
     "No population figure. Per-capita cannot be computed from this schema."),
    ("U09", "Which grantees are subcontractors to other grantees?",
     "No grantee-to-grantee relationship table."),
    ("U10", "What is each grantee's credit rating?",
     "No credit, risk or rating column."),
]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ambiguous — 8 questions with more than one honest answer
# MAGIC
# MAGIC These are the ones the project is really about, and they are the reason it is worth
# MAGIC presenting rather than just running.
# MAGIC
# MAGIC An unanswerable question has a clean right behaviour: decline. An **ambiguous**
# MAGIC question is one where the data supports two or three different, defensible numbers,
# MAGIC and picking one silently is how a stakeholder ends up with a figure that does not
# MAGIC mean what they think it means. Nobody sees an error, because there isn't one.
# MAGIC
# MAGIC `M06` is the sharpest: "funding for the Northeast" can read as *awards made by
# MAGIC Northeast-region agencies* or *awards to grantees located in northeastern states*.
# MAGIC Both are legitimate readings of the schema, and they return different totals.

# COMMAND ----------

AMBIGUOUS = [
    ("M01", "Show me last year's awards.",
     "No as-of date is defined, and 'year' could be fiscal_year on the program or the "
     "calendar year of award_date. These give different sets."),
    ("M02", "Who are our top grantees?",
     "Top by total awarded, by number of awards, or by amount actually paid out? And how many?"),
    ("M03", "How much money has gone out the door?",
     "awards.award_amount and disbursements.amount are both present and differ. "
     "Committed and paid are not the same number."),
    ("M04", "List the big awards.",
     "No threshold is given and the schema implies none."),
    ("M05", "What's the average grant?",
     "Mean per award, per grantee or per program, and whether Suspended awards count."),
    ("M06", "Show me funding for the Northeast.",
     "agencies.region is an AGENCY attribute; grantees.state is the grantee location. "
     "Both readings are defensible and return different totals."),
    ("M07", "How many active grants do we have?",
     "status = 'Active', or awards with a recent disbursement? These differ."),
    ("M08", "Compare this year to last year.",
     "No as-of date, no stated measure, and no stated definition of year."),
]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write the bank to Delta
# MAGIC
# MAGIC The bank lives in the catalogue rather than in this notebook's variables, so notebooks
# MAGIC `03` and `04` read the same rows this notebook wrote. A question bank that exists only
# MAGIC as Python in one notebook cannot be joined to results, versioned, or diffed when it
# MAGIC changes.

# COMMAND ----------

import re

from pyspark.sql import Row


def tidy(sql: str | None) -> str | None:
    """Collapse the indentation the triple-quoted strings carry, and nothing else.

    The SQL is not rewritten or normalised beyond whitespace. The grader compares what
    the query RETURNS, never how it is written -- see notebook 02.
    """
    if sql is None:
        return None
    return re.sub(r"\s+", " ", sql).strip()


rows = []
for qid, text, ordered, sql in ANSWERABLE:
    rows.append(Row(question_id=qid, population="answerable", question_text=text,
                    gold_sql=tidy(sql), order_matters=ordered, note=None))
for qid, text, note in UNANSWERABLE:
    rows.append(Row(question_id=qid, population="unanswerable", question_text=text,
                    gold_sql=None, order_matters=False, note=note))
for qid, text, note in AMBIGUOUS:
    rows.append(Row(question_id=qid, population="ambiguous", question_text=text,
                    gold_sql=None, order_matters=False, note=note))

spark.sql("""
CREATE OR REPLACE TABLE questions (
  question_id   STRING  COMMENT 'Stable id. A01-A12 answerable, U01-U10 unanswerable, M01-M08 ambiguous.',
  population    STRING  COMMENT 'answerable | unanswerable | ambiguous. Decides how the question is graded.',
  question_text STRING  COMMENT 'The question as a stakeholder would ask it. This is the ONLY field shown to the model.',
  gold_sql      STRING  COMMENT 'Expected query, answerable questions only. Graded on the rows it RETURNS, never on its text.',
  order_matters BOOLEAN COMMENT 'True where the ordering is part of the answer, such as a top-N. Rows are compared as an unordered multiset otherwise.',
  note          STRING  COMMENT 'Reviewer note: what is missing, or what is ambiguous. NEVER shown to the model.'
) COMMENT 'The evaluation question bank. Three populations, graded separately and never pooled into one accuracy figure.'
""")

spark.createDataFrame(rows, schema=spark.table("questions").schema) \
     .write.mode("overwrite").insertInto("questions")

display(spark.sql("SELECT population, count(*) AS questions FROM questions GROUP BY population ORDER BY population"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validate the bank before trusting a single score from it
# MAGIC
# MAGIC Four checks. Any failure raises, because each one would otherwise corrupt every
# MAGIC score computed afterwards and do it quietly.
# MAGIC
# MAGIC 1. **Every expected query runs.** A gold query with a typo marks a correct model
# MAGIC    answer wrong forever.
# MAGIC 2. **No expected answer is empty.** An empty expected answer credits any wrong query
# MAGIC    that also returns nothing — and `SELECT … WHERE 1=0` returns nothing.
# MAGIC 3. **Only answerable questions have expected SQL.** Gold SQL on an unanswerable
# MAGIC    question means the question is answerable and is mislabelled.
# MAGIC 4. **Question ids are unique**, or results cannot be joined back to the bank.

# COMMAND ----------

failures = []

bank = spark.table("questions").collect()

for row in bank:
    if row.population == "answerable":
        if not row.gold_sql:
            failures.append(f"{row.question_id}: answerable with no gold_sql")
            continue
        try:
            n = spark.sql(row.gold_sql).count()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{row.question_id}: gold_sql failed: {exc}")
            continue
        if n == 0:
            failures.append(f"{row.question_id}: gold_sql returns 0 rows -- "
                            "an empty expected answer cannot be graded honestly")
    elif row.gold_sql:
        failures.append(f"{row.question_id}: {row.population} must not have gold_sql")

ids = [r.question_id for r in bank]
if len(ids) != len(set(ids)):
    failures.append("duplicate question_id in the bank")

counts = {p: sum(1 for r in bank if r.population == p)
          for p in ("answerable", "unanswerable", "ambiguous")}
print(f"bank: {len(bank)} questions {counts}")

if failures:
    raise AssertionError("question bank is not valid:\n  " + "\n  ".join(failures))
print("all four bank checks passed")
