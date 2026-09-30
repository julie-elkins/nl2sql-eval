# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Build the synthetic grant-awards schema
# MAGIC
# MAGIC Run this once. It creates five Delta tables in Unity Catalog, fully commented, and
# MAGIC tags the one column that looks like personal data.
# MAGIC
# MAGIC ## What this notebook is for
# MAGIC
# MAGIC The project measures whether a natural-language-to-SQL system knows what it cannot
# MAGIC answer. That only works if the schema has **deliberate holes** — real questions a
# MAGIC stakeholder would ask that the data genuinely cannot support. So the absences below
# MAGIC are the design, not an oversight.
# MAGIC
# MAGIC **This schema has no:** employee or headcount figures, performance or outcome
# MAGIC measures, application or denial records, appropriation source, audit findings,
# MAGIC approval timestamps, forecasts, or population counts.
# MAGIC
# MAGIC Notebook `01` asks for exactly those things.
# MAGIC
# MAGIC ## Why the data is synthetic, stated plainly
# MAGIC
# MAGIC Every row here is a closed-form function of a row index — no real organisation, no
# MAGIC employer material, no customer data. Contact addresses use the `.invalid` domain,
# MAGIC which RFC 2606 reserves permanently, so the column that looks like personal data
# MAGIC **cannot reach anyone** while still being worth governing.
# MAGIC
# MAGIC Arithmetic rather than a seeded random generator: the dataset is explicable by
# MAGIC reading this file, and identical on every runtime version.

# COMMAND ----------

CATALOG = "workspace"
SCHEMA = "nl2sql_eval"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA} COMMENT "
          "'Synthetic grant-awards data and an NL-to-SQL evaluation harness. "
          "No real organisation or person appears in any row.'")
spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")

print(f"Working in {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The schema, declared once with its comments
# MAGIC
# MAGIC The column comments are **load-bearing**, not documentation. Notebook `03` builds the
# MAGIC model's prompt by querying `information_schema.columns`, so these comments *are* what
# MAGIC the model is told about the data.
# MAGIC
# MAGIC That is the whole reason to declare them here rather than writing a schema
# MAGIC description by hand in the prompt. A hand-written description drifts from the tables,
# MAGIC and the drift fails in a way that flatters the model: it gets told about a column that
# MAGIC does not exist, writes reasonable SQL against it, and the run records a SQL error that
# MAGIC everybody reads as the model's fault. Generating the description from the catalogue
# MAGIC makes that impossible.
# MAGIC
# MAGIC Two comments below are deliberately the traps that notebook `01`'s ambiguous questions
# MAGIC turn on — that `region` belongs to the *agency* and not the grantee, and that
# MAGIC disbursements need not sum to the award amount.

# COMMAND ----------

spark.sql("""
CREATE OR REPLACE TABLE agencies (
  agency_id   INT    COMMENT 'Primary key.',
  agency_name STRING COMMENT 'Agency display name.',
  region      STRING COMMENT 'The AGENCY administrative region. This is not the grantee location; a grantee location is grantees.state.'
) COMMENT 'Awarding agencies. One row per agency.'
""")

spark.sql("""
CREATE OR REPLACE TABLE programs (
  program_id        INT           COMMENT 'Primary key.',
  program_name       STRING        COMMENT 'Program display name.',
  agency_id          INT           COMMENT 'Foreign key to agencies.agency_id.',
  fiscal_year        INT           COMMENT 'Fiscal year the program is authorised for, as an integer such as 2024. There is no start or end date for a fiscal year anywhere in this schema.',
  authorized_amount  DECIMAL(14,2) COMMENT 'Dollars the program is authorised to award in its fiscal year.'
) COMMENT 'Grant programs. One row per program per fiscal year.'
""")

spark.sql("""
CREATE OR REPLACE TABLE grantees (
  grantee_id    INT    COMMENT 'Primary key.',
  grantee_name  STRING COMMENT 'Grantee display name.',
  state         STRING COMMENT 'Two-letter state code where the grantee is located.',
  grantee_type  STRING COMMENT 'One of City, County, Tribal, Nonprofit, University.',
  contact_email STRING COMMENT 'Contact address. Synthetic and permanently undeliverable: every value is on the RFC 2606 reserved .invalid domain.'
) COMMENT 'Organisations that receive awards. One row per grantee.'
""")

spark.sql("""
CREATE OR REPLACE TABLE awards (
  award_id     INT           COMMENT 'Primary key.',
  program_id   INT           COMMENT 'Foreign key to programs.program_id.',
  grantee_id   INT           COMMENT 'Foreign key to grantees.grantee_id.',
  award_date   DATE          COMMENT 'Calendar date the award was made.',
  award_amount DECIMAL(14,2) COMMENT 'Dollars awarded.',
  status       STRING        COMMENT 'One of Active, Closed, Suspended.'
) COMMENT 'Awards made to grantees under a program. One row per award.'
""")

spark.sql("""
CREATE OR REPLACE TABLE disbursements (
  disbursement_id   INT           COMMENT 'Primary key.',
  award_id          INT           COMMENT 'Foreign key to awards.award_id.',
  disbursement_date DATE          COMMENT 'Calendar date the payment was made.',
  amount            DECIMAL(14,2) COMMENT 'Dollars paid.'
) COMMENT 'Payments against an award. An award has one to three disbursements, and their amounts do NOT necessarily sum to the award amount.'
""")

print("Five tables created with comments.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Tag the column that looks like personal data
# MAGIC
# MAGIC `contact_email` is synthetic and undeliverable, so nothing here is at risk. It is
# MAGIC tagged anyway, because the governance question a public-sector buyer asks is not
# MAGIC "is this data sensitive" but "**can you show me which columns you classified, and
# MAGIC when**". A tag is queryable from `information_schema`; a note in a document is not.
# MAGIC
# MAGIC If tagging is unavailable on this tier the cell reports that and continues — it does
# MAGIC not silently pass. An ungoverned column recorded as governed is worse than an
# MAGIC ungoverned column.

# COMMAND ----------

try:
    spark.sql("ALTER TABLE grantees ALTER COLUMN contact_email "
              "SET TAGS ('data_classification' = 'contact_information')")
    tag_state = "APPLIED"
except Exception as exc:  # noqa: BLE001 - the message is the finding
    tag_state = f"NOT APPLIED: {type(exc).__name__}: {exc}"

print(f"contact_email classification tag: {tag_state}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Generate the rows
# MAGIC
# MAGIC Every value is arithmetic on the row index. Notes on two choices that are not
# MAGIC arbitrary:
# MAGIC
# MAGIC - `grantee_id` steps by **7** across **40** grantees. Those are coprime, so the step
# MAGIC   visits all 40 before repeating instead of landing on a subset.
# MAGIC - A **Closed** award is paid in full and the split takes its rounding remainder on
# MAGIC   the last payment, so the parts sum *exactly*. Active and Suspended awards are paid
# MAGIC   25% per disbursement. That is what makes "which awards are not fully paid out" a
# MAGIC   question with a real, non-empty answer — and notebook `02` explains why an
# MAGIC   expected answer of zero rows would be a hole in the grading rather than a question.

# COMMAND ----------

import datetime as dt
from decimal import Decimal

N_PROGRAMS, N_GRANTEES, N_AWARDS = 24, 40, 400

AGENCY_NAMES = (
    "Department of Transportation",
    "Department of Energy",
    "Environmental Protection Agency",
    "Department of Health and Human Services",
    "Department of Education",
    "Economic Development Administration",
)
REGIONS = ("Northeast", "Southeast", "Midwest", "Mountain", "Pacific", "National")
PROGRAM_TOPICS = (
    "Bridge Resilience", "Grid Modernization", "Watershed Restoration",
    "Rural Health Access", "Workforce Training", "Broadband Expansion",
    "Transit Electrification", "Brownfield Cleanup",
)
STATES = ("AZ", "CA", "CO", "GA", "MI", "NC", "NM", "OH", "OR", "PA")
GRANTEE_TYPES = ("City", "County", "Tribal", "Nonprofit", "University")
# Five-long cycle, so exactly 3/5 Active, 1/5 Closed, 1/5 Suspended.
STATUS_CYCLE = ("Active", "Active", "Active", "Closed", "Suspended")
PARTIAL_SHARE = Decimal("0.25")


def money(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


agency_rows = [
    (i, AGENCY_NAMES[i - 1], REGIONS[(i - 1) % len(REGIONS)])
    for i in range(1, len(AGENCY_NAMES) + 1)
]

program_rows = []
for i in range(1, N_PROGRAMS + 1):
    fiscal_year = 2023 + (i - 1) // 8
    program_rows.append((
        100 + i,
        f"{PROGRAM_TOPICS[(i - 1) % len(PROGRAM_TOPICS)]} {fiscal_year}",
        ((i - 1) % len(AGENCY_NAMES)) + 1,
        fiscal_year,
        money(2_000_000 + i * 350_000),
    ))

grantee_rows = []
for i in range(1, N_GRANTEES + 1):
    state = STATES[(i - 1) % len(STATES)]
    gtype = GRANTEE_TYPES[(i - 1) % len(GRANTEE_TYPES)]
    gid = 1000 + i
    grantee_rows.append((
        gid, f"{state} {gtype} Authority {i:02d}", state, gtype,
        f"grants{gid}@example.invalid",
    ))

fiscal_by_program = {p[0]: p[3] for p in program_rows}

award_rows = []
for i in range(1, N_AWARDS + 1):
    program_id = 101 + ((i - 1) % N_PROGRAMS)
    fiscal_year = fiscal_by_program[program_id]
    award_rows.append((
        5000 + i,
        program_id,
        1001 + ((i * 7) % N_GRANTEES),
        dt.date(fiscal_year, ((i - 1) % 12) + 1, ((i * 3) % 27) + 1),
        money(25_000 + ((i * 1337) % 47) * 10_000),
        STATUS_CYCLE[(i - 1) % len(STATUS_CYCLE)],
    ))

disbursement_rows = []
disbursement_id = 9000
for award_id, _prog, _grantee, award_date, amount, status in award_rows:
    n = (award_id % 3) + 1
    if status == "Closed":
        part = money(amount / n)
        parts = [part] * (n - 1) + [money(amount - part * (n - 1))]
    else:
        parts = [money(amount * PARTIAL_SHARE)] * n
    for k, part in enumerate(parts):
        disbursement_id += 1
        disbursement_rows.append((
            disbursement_id, award_id, award_date + dt.timedelta(days=45 * k), part,
        ))

print(f"generated: {len(agency_rows)} agencies, {len(program_rows)} programs, "
      f"{len(grantee_rows)} grantees, {len(award_rows)} awards, "
      f"{len(disbursement_rows)} disbursements")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write the rows
# MAGIC
# MAGIC `insertInto` rather than `saveAsTable`: `saveAsTable` would overwrite the table
# MAGIC definition and **silently drop every column comment** created above — which would
# MAGIC take the model's schema description with it, since notebook `03` reads the comments
# MAGIC from the catalogue. `insertInto` writes rows into the table that already exists.

# COMMAND ----------

payload = {
    "agencies": agency_rows,
    "programs": program_rows,
    "grantees": grantee_rows,
    "awards": award_rows,
    "disbursements": disbursement_rows,
}

for table, rows in payload.items():
    # Take the schema from the table we just declared, so the DataFrame cannot be typed
    # differently from its destination.
    target_schema = spark.table(table).schema
    spark.createDataFrame(rows, schema=target_schema) \
         .write.mode("overwrite").insertInto(table)

for table in payload:
    print(f"{table:16s} {spark.table(table).count():>5d} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify the comments survived
# MAGIC
# MAGIC This is not a formality. If the comments are gone, notebook `03` builds an empty
# MAGIC schema description, the model is handed nothing to work with, and the run reports a
# MAGIC terrible score that has nothing to do with the model. **A count of zero here means
# MAGIC stop and fix it**, not carry on.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT table_name,
# MAGIC        count(*)                                          AS columns,
# MAGIC        count(comment)                                    AS columns_with_comments,
# MAGIC        count(*) - count(comment)                         AS missing
# MAGIC FROM   workspace.information_schema.columns
# MAGIC WHERE  table_schema = 'nl2sql_eval'
# MAGIC   AND  table_name IN ('agencies','programs','grantees','awards','disbursements')
# MAGIC GROUP  BY table_name
# MAGIC ORDER  BY table_name
