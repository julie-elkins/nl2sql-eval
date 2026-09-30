# Databricks notebook source
# MAGIC %md
# MAGIC # `_harness` — shared definitions
# MAGIC
# MAGIC **Do not run this notebook to get a result.** It defines things and asserts nothing.
# MAGIC Notebooks `02`, `03` and `04` pull it in with `%run ./_harness`.
# MAGIC
# MAGIC One file so the grader that is calibrated in `02` is byte-for-byte the grader that
# MAGIC scores the model in `03`. If each notebook carried its own copy, calibrating one and
# MAGIC scoring with another would be the easiest possible way to publish a number nobody
# MAGIC checked.

# COMMAND ----------

import datetime as dt
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal

CATALOG, SCHEMA = "workspace", "nl2sql_eval"
spark.sql(f"USE CATALOG {CATALOG}")
spark.sql(f"USE SCHEMA {SCHEMA}")

#: Most rows collected from a model's query. A model can write a cross join, and a
#: cross join here is 400 x 800 rows. The cap is one MORE than any expected answer, so
#: hitting it is always a finding rather than a truncation nobody notices.
ROW_CAP = 5000

# COMMAND ----------

# MAGIC %md
# MAGIC ## The schema description, generated from Unity Catalog
# MAGIC
# MAGIC The model is told about the tables by querying `information_schema.columns`, so the
# MAGIC description cannot drift from the tables that exist. Hand-writing it into the prompt
# MAGIC is the obvious alternative and it fails in the direction that flatters the model: it
# MAGIC gets told about a column that was renamed, writes reasonable SQL against it, and the
# MAGIC run records a SQL error everybody reads as the model's mistake.

# COMMAND ----------

TABLES = ("agencies", "programs", "grantees", "awards", "disbursements")


def schema_card() -> str:
    """Return the schema description handed to the model, read from the catalogue."""
    rows = spark.sql(f"""
        SELECT table_name, column_name, full_data_type, comment, ordinal_position
        FROM   {CATALOG}.information_schema.columns
        WHERE  table_schema = '{SCHEMA}'
          AND  table_name IN {TABLES}
        ORDER BY table_name, ordinal_position
    """).collect()

    if not rows:
        raise RuntimeError(
            "information_schema returned no columns for this schema. Run notebook 00 "
            "first. Continuing would hand the model an empty schema and blame it for "
            "the score."
        )

    by_table: dict[str, list] = {}
    for r in rows:
        by_table.setdefault(r.table_name, []).append(r)

    out = []
    for table in TABLES:
        cols = by_table.get(table, [])
        out.append(f"TABLE {table}")
        for c in cols:
            note = f" -- {c.comment}" if c.comment else ""
            out.append(f"    {c.column_name} {c.full_data_type}{note}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# COMMAND ----------

# MAGIC %md
# MAGIC ## The prompt, and why the model must answer in a structured field
# MAGIC
# MAGIC The model returns JSON with an explicit `answerable` boolean. The harness reads **that
# MAGIC field**. It never decides "this looks like a refusal" by searching the prose for
# MAGIC "I can't" or "unfortunately".
# MAGIC
# MAGIC That is not fussiness, it is the difference between a valid eval and an invalid one.
# MAGIC Sniffing prose fails in the flattering direction: a reply that hedges in a paragraph
# MAGIC and then supplies a confident query would score as a refusal, so the single failure
# MAGIC this project exists to catch — inventing an answer to an unanswerable question — would
# MAGIC be recorded as a success.
# MAGIC
# MAGIC The prompt is hashed and the hash is stored with every result, so two runs can be
# MAGIC compared only when they were asked the same way.

# COMMAND ----------

PROMPT_TEMPLATE = """You answer questions about a grant-awards database by writing SQL.

You may only use the tables and columns listed below. Nothing else exists.

{schema}
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


def build_prompt(question_text: str, schema: str, template: str = PROMPT_TEMPLATE) -> str:
    return template.format(schema=schema, question=question_text)


def prompt_fingerprint(schema: str, template: str = PROMPT_TEMPLATE) -> str:
    """Hash of the template AND the schema description, since both shape the answer.

    The template is a parameter rather than the module constant so that a run using a
    prompt variant records the variant's fingerprint. Hashing the default while running
    something else would label two different experiments identically, which is worse
    than recording no fingerprint at all -- it makes an incomparable pair look
    comparable.
    """
    payload = (template + "\x00" + schema).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


# COMMAND ----------

# MAGIC %md
# MAGIC ## Parsing the reply
# MAGIC
# MAGIC A reply the harness cannot parse gets its own verdict, `unparseable`. It is **never**
# MAGIC counted as a refusal.
# MAGIC
# MAGIC Why that line matters: on the unanswerable half of the bank, declining is the correct
# MAGIC answer. If unparseable output were treated as declining, a model that returned an
# MAGIC empty string every time would score 100% on ten of thirty questions for producing
# MAGIC nothing at all.

# COMMAND ----------

@dataclass(frozen=True)
class ModelResponse:
    parse_ok: bool
    answerable: bool | None = None
    sql: str | None = None
    reason: str | None = None
    raw: str = ""
    provider_error: str | None = None

    @property
    def declined(self) -> bool:
        """True only for a well-formed reply that declined. Never true for garbage."""
        return self.parse_ok and self.answerable is False

    @property
    def attempted(self) -> bool:
        return self.parse_ok and self.answerable is True and bool(self.sql)


def parse_response(raw: str) -> ModelResponse:
    """Pull the JSON object out of a reply.

    Tolerant of a fenced code block or surrounding chatter, because that is a formatting
    habit rather than a reasoning failure and penalising it would measure the wrong
    thing. NOT tolerant of a missing `answerable` field -- that IS the answer.
    """
    if raw is None or not str(raw).strip():
        return ModelResponse(parse_ok=False, raw="")

    text = str(raw).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return ModelResponse(parse_ok=False, raw=text)

    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ModelResponse(parse_ok=False, raw=text)

    if not isinstance(obj, dict) or not isinstance(obj.get("answerable"), bool):
        # The field is the whole contract. Absent or non-boolean means unparseable, not
        # a guess at what was meant.
        return ModelResponse(parse_ok=False, raw=text)

    sql = obj.get("sql")
    reason = obj.get("reason")
    return ModelResponse(
        parse_ok=True,
        answerable=obj["answerable"],
        sql=str(sql).strip() if isinstance(sql, str) and sql.strip() else None,
        reason=str(reason).strip() if isinstance(reason, str) and reason.strip() else None,
        raw=text,
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## Read-only enforcement
# MAGIC
# MAGIC The harness executes SQL a language model wrote. It checks first that the query only
# MAGIC reads.
# MAGIC
# MAGIC This is a control, not a lint. A natural-language-to-SQL feature that can be talked
# MAGIC into `DELETE FROM awards` is a finding on any security review, and "the prompt says
# MAGIC read only" is not a control — the prompt is a request to the thing you do not trust.
# MAGIC The check runs on the SQL that is about to execute.
# MAGIC
# MAGIC It is deliberately a blunt allow-list on the leading keyword plus a deny-list of
# MAGIC statement words, and it will refuse some harmless queries. Refusing a safe query
# MAGIC costs one question's score; running an unsafe one costs the table.

# COMMAND ----------

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"REPLACE|REFRESH|COPY|OPTIMIZE|VACUUM|SET|USE|CALL)\b",
    re.IGNORECASE,
)


def read_only_violation(sql: str) -> str | None:
    """Return a reason to refuse this SQL, or None if it only reads."""
    if ";" in sql.rstrip().rstrip(";"):
        return "multiple statements"
    head = sql.lstrip().lstrip("(").lstrip()
    if not re.match(r"^(SELECT|WITH)\b", head, re.IGNORECASE):
        return "does not begin with SELECT or WITH"
    hit = _FORBIDDEN.search(sql)
    if hit:
        return f"contains {hit.group(1).upper()}"
    return None


# COMMAND ----------

# MAGIC %md
# MAGIC ## Comparing results
# MAGIC
# MAGIC Three decisions, each of which changes what the score means.
# MAGIC
# MAGIC **Compare what the query returns, never its text.** Two correct queries for the same
# MAGIC question look nothing alike. String-matching SQL measures whether the model writes in
# MAGIC the same style as whoever wrote the expected answer, which is not a capability anyone
# MAGIC is buying.
# MAGIC
# MAGIC **Ignore column names, honour column count and position.** An alias is not the
# MAGIC answer, so `total` and `total_awarded` are the same result. An extra column is a
# MAGIC different answer.
# MAGIC
# MAGIC **Ignore row order, except where ordering is the question.** A "top five" whose order
# MAGIC is ignored is not graded at all — any five of the forty grantees would pass. That is
# MAGIC what `order_matters` in the bank controls.

# COMMAND ----------

def _norm(value):
    """Make one cell comparable across engines and numeric types.

    All numbers become floats rounded to 2dp: `count(*)` returns an integer and a model
    may return `5.0`, and grading those as different answers would be measuring the
    return type rather than the number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 2)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def _sort_key(row):
    return tuple((v is None, str(v)) for v in row)


def run_sql(sql: str) -> tuple[list[tuple], str | None, bool]:
    """Execute read-only SQL. Returns (rows, error, hit_cap)."""
    violation = read_only_violation(sql)
    if violation:
        return [], f"refused as not read-only: {violation}", False
    try:
        collected = spark.sql(sql).limit(ROW_CAP + 1).collect()
    except Exception as exc:  # noqa: BLE001 - the message is the result
        return [], f"{type(exc).__name__}: {exc}", False
    hit_cap = len(collected) > ROW_CAP
    rows = [tuple(_norm(v) for v in r) for r in collected[:ROW_CAP]]
    return rows, None, hit_cap


def results_match(actual: list[tuple], expected: list[tuple], order_matters: bool) -> bool:
    if len(actual) != len(expected):
        return False
    if order_matters:
        return actual == expected
    return sorted(actual, key=_sort_key) == sorted(expected, key=_sort_key)


# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict vocabulary
# MAGIC
# MAGIC Nine outcomes, kept separate on purpose. The pairs that are usually collapsed are the
# MAGIC ones worth keeping apart:
# MAGIC
# MAGIC - `wrong_result` vs `sql_error` — a query that errors is caught by whoever ran it. A
# MAGIC   query that returns the wrong number is not caught by anyone.
# MAGIC - `invented_answer` vs `wrongly_declined` — over-claiming and over-refusing are
# MAGIC   opposite failures with opposite fixes. One number cannot show both.
# MAGIC - `provider_error` vs everything else — a rate limit is not a model behaviour, and
# MAGIC   pooling them lets an outage read as a score.

# COMMAND ----------

CORRECT_BY_POPULATION = {
    "answerable": "correct",
    "unanswerable": "correct_decline",
    "ambiguous": "correct_clarification",
}


def grade(question, response: ModelResponse) -> dict:
    """Grade one reply to one question. Returns a row ready for the results table."""
    out = {
        "question_id": question.question_id,
        "population": question.population,
        "verdict": None,
        "correct": False,
        "rows_returned": None,
        "rows_expected": None,
        "model_sql": response.sql,
        "model_reason": response.reason,
        "error": None,
        "raw_response": response.raw,
    }

    if response.provider_error:
        out["verdict"] = "provider_error"
        out["error"] = response.provider_error
        return out

    if not response.parse_ok:
        out["verdict"] = "unparseable"
        return out

    # --- the model declined ---------------------------------------------------------
    if response.declined:
        if not response.reason:
            # A presence check on a field, not a judgement about the prose. A bare "no"
            # is not usable output: a stakeholder cannot act on it.
            out["verdict"] = "declined_without_reason"
            return out
        if question.population == "answerable":
            out["verdict"] = "wrongly_declined"
            return out
        out["verdict"] = CORRECT_BY_POPULATION[question.population]
        out["correct"] = True
        return out

    # --- the model produced SQL -----------------------------------------------------
    if not response.attempted:
        # Claimed answerable and supplied no query. Its own contract, unmet.
        out["verdict"] = "unparseable"
        return out

    if question.population == "unanswerable":
        out["verdict"] = "invented_answer"
        return out
    if question.population == "ambiguous":
        out["verdict"] = "answered_ambiguous"
        return out

    actual, error, hit_cap = run_sql(response.sql)
    if error:
        out["verdict"] = "sql_error"
        out["error"] = error
        return out

    expected, gold_error, _ = run_sql(question.gold_sql)
    if gold_error:
        # The expected answer itself failed. That is a harness fault and must never be
        # charged to the model -- notebook 01 validates the bank to stop this happening.
        out["verdict"] = "provider_error"
        out["error"] = f"gold_sql failed: {gold_error}"
        return out

    out["rows_returned"] = len(actual)
    out["rows_expected"] = len(expected)
    if hit_cap:
        out["verdict"] = "wrong_result"
        out["error"] = f"returned more than {ROW_CAP} rows"
        return out

    if results_match(actual, expected, bool(question.order_matters)):
        out["verdict"] = "correct"
        out["correct"] = True
    else:
        out["verdict"] = "wrong_result"
    return out


# COMMAND ----------

# MAGIC %md
# MAGIC ## Calibration providers — five fake models with known right answers
# MAGIC
# MAGIC These call nothing and cost nothing. Each one has a score the grader **must** give
# MAGIC it, and notebook `02` asserts all fifteen cells. They exist because a grader is
# MAGIC itself a piece of software that can be wrong, and a broken grader produces a clean,
# MAGIC confident, wrong number that looks exactly like a finding.

# COMMAND ----------

def provider_gold_echo(question, _prompt) -> ModelResponse:
    """Returns the expected query when there is one, and a wrong query otherwise.

    Proves the grader can recognise a right answer -- the no-false-red end of the
    calibration. It must score 0 on both non-answerable populations, because there it is
    claiming answerability it does not have.
    """
    if question.population == "answerable":
        return ModelResponse(True, answerable=True, sql=question.gold_sql, raw="<gold>")
    return ModelResponse(True, answerable=True,
                         sql="SELECT count(*) AS n FROM awards", raw="<gold:wrong>")


def provider_always_decline(_question, _prompt) -> ModelResponse:
    """Declines everything, with a reason.

    Proves the two halves are graded independently. Scores 0 on answerable and full
    marks on both non-answerable populations. A model that looked like this would be
    useless, and the point is that a single pooled accuracy figure would not say so.
    """
    return ModelResponse(True, answerable=False,
                         reason="Cannot be answered from this schema.", raw="<decline>")


def provider_always_wrong(_question, _prompt) -> ModelResponse:
    """Valid SQL, never the right answer. The all-red floor: 0, 0, 0.

    It returns one row containing -1 rather than zero rows, deliberately. A query that
    returns nothing would match any expected answer that also returns nothing, so an
    empty-result probe would silently pass wherever the bank had an empty gold answer.
    Notebook 01 forbids empty gold answers; this provider is the belt to that braces.
    """
    return ModelResponse(True, answerable=True,
                         sql="SELECT CAST(-1 AS BIGINT) AS sentinel", raw="<wrong>")


def provider_garbage(_question, _prompt) -> ModelResponse:
    """Returns prose, no JSON. Must score 0, 0, 0 -- all `unparseable`.

    The one that matters most. If unparseable were credited as a decline this provider
    would score full marks on eighteen of thirty questions for producing nothing usable.
    """
    return parse_response("Sure! Let me help with that. The answer depends on the data.")


def provider_decline_no_reason(_question, _prompt) -> ModelResponse:
    """Declines with an empty reason. Must score 0, 0, 0.

    Proves the reason field is actually required, so "no" alone cannot earn the marks
    that a useful "no, because X is missing" earns.
    """
    return ModelResponse(True, answerable=False, reason=None, raw="<decline:bare>")


CALIBRATION_PROVIDERS = {
    "gold_echo": provider_gold_echo,
    "always_decline": provider_always_decline,
    "always_wrong": provider_always_wrong,
    "garbage": provider_garbage,
    "decline_no_reason": provider_decline_no_reason,
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## Running a bank, and what gets recorded with every row
# MAGIC
# MAGIC Provenance is not bookkeeping here — it is what makes a second run comparable to a
# MAGIC first. A score without the prompt hash, the model name and the bank version beside it
# MAGIC cannot be compared to anything later, and "we changed one thing and it improved" is
# MAGIC not checkable after the fact.

# COMMAND ----------

RESULTS_TABLE = "eval_results"


def ensure_results_table() -> None:
    spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
      run_id        STRING  COMMENT 'One id per execution of the whole bank.',
      run_label     STRING  COMMENT 'Human label, e.g. baseline or null-run-b.',
      run_started   TIMESTAMP,
      provider      STRING  COMMENT 'Model endpoint, or the name of a calibration provider.',
      prompt_sha    STRING  COMMENT 'Hash of the prompt template AND the generated schema description. Two runs are comparable only if this matches.',
      bank_size     INT     COMMENT 'Questions in the bank at run time.',
      question_id   STRING,
      population    STRING,
      verdict       STRING  COMMENT 'One of nine outcomes. Never pooled across populations.',
      correct       BOOLEAN,
      rows_returned INT,
      rows_expected INT,
      model_sql     STRING,
      model_reason  STRING,
      error         STRING,
      raw_response  STRING  COMMENT 'The reply verbatim, so a disputed verdict is re-read rather than re-run.'
    ) COMMENT 'One row per question per run. Append only.'
    """)


def run_bank(provider_name, provider_fn, run_label: str, schema: str | None = None,
             template: str = PROMPT_TEMPLATE) -> str:
    """Run every question through one provider, grade it, append the rows. Returns run_id."""
    ensure_results_table()
    schema = schema if schema is not None else schema_card()
    fingerprint = prompt_fingerprint(schema, template)
    run_id = uuid.uuid4().hex[:12]
    started = dt.datetime.now()

    bank = spark.table("questions").orderBy("question_id").collect()
    rows = []
    for question in bank:
        prompt = build_prompt(question.question_text, schema, template)
        try:
            response = provider_fn(question, prompt)
        except Exception as exc:  # noqa: BLE001
            response = ModelResponse(False, provider_error=f"{type(exc).__name__}: {exc}")
        graded = grade(question, response)
        graded.update(
            run_id=run_id, run_label=run_label, run_started=started,
            provider=provider_name, prompt_sha=fingerprint, bank_size=len(bank),
        )
        rows.append(graded)

    frame = spark.createDataFrame(rows, schema=spark.table(RESULTS_TABLE).schema)
    frame.write.mode("append").insertInto(RESULTS_TABLE)
    return run_id


def score_by_population(run_id: str) -> dict[str, tuple[int, int]]:
    """{population: (correct, total)} for one run."""
    rows = spark.sql(f"""
        SELECT population, sum(CASE WHEN correct THEN 1 ELSE 0 END) AS c, count(*) AS n
        FROM   {RESULTS_TABLE} WHERE run_id = '{run_id}' GROUP BY population
    """).collect()
    return {r.population: (int(r.c), int(r.n)) for r in rows}


# COMMAND ----------

# MAGIC %md
# MAGIC ## Reaching a real model
# MAGIC
# MAGIC Endpoint naming differs between Databricks tiers and moves over time — on Free Edition
# MAGIC foundation models sit under Unity Gateway rather than as serving endpoints of their own.
# MAGIC So `find_endpoint` probes a short list with a throwaway prompt and reports which names
# MAGIC this workspace will actually serve today. Cheaper than reading documentation and more
# MAGIC reliable, because it reports the only fact that matters.

# COMMAND ----------

ENDPOINT_CANDIDATES = [
    "gemma-3-12b",
    "databricks-gemma-3-12b",
    "databricks-gpt-oss-120b",
    "databricks-qwen3-next-instruct",
    "databricks-llama-4-maverick",
]


def probe_endpoint(name: str) -> tuple[bool, str]:
    try:
        row = spark.sql("SELECT ai_query(:ep, :req) AS raw",
                        args={"ep": name, "req": "Reply with the single word: ok"}).collect()[0]
        return True, (row.raw or "")[:120]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def find_endpoint(candidates=None, verbose: bool = True) -> str:
    """Return the first candidate endpoint that answers. Raises if none do."""
    usable = []
    for candidate in (candidates or ENDPOINT_CANDIDATES):
        ok, detail = probe_endpoint(candidate)
        if verbose:
            print(f"{'OK  ' if ok else 'FAIL'} {candidate:32s} {detail}")
        if ok:
            usable.append(candidate)
    if not usable:
        raise RuntimeError(
            "No endpoint answered. Open AI/ML > AI Gateway in the sidebar, note the exact "
            "model name listed there, and add it to ENDPOINT_CANDIDATES. Do not proceed by "
            "guessing a name."
        )
    return usable[0]


def make_model_provider(endpoint: str, temperature: float = 0.0,
                        max_tokens: int = 600, with_parameters: bool = True):
    """Build a provider that asks `endpoint` one question and parses the reply.

    THE PROMPT IS A QUERY PARAMETER, never concatenated into the SQL string. It contains
    quotes, newlines and braces, so concatenation produces syntax errors on some
    questions and not others -- which reads exactly like the model failing those
    questions. It is also the same mistake as SQL injection, in a project about a model
    writing SQL.

    `temperature = 0` reduces variation and does NOT give determinism. Nothing here
    assumes it does: notebook 04 measures the variation that is left rather than
    asserting there is none.
    """
    if with_parameters:
        query = """
            SELECT ai_query(:ep, :req,
                     modelParameters => named_struct('temperature', :temp,
                                                     'max_tokens', :maxtok)) AS raw
        """
        args = {"ep": endpoint, "temp": temperature, "maxtok": max_tokens}
    else:
        query = "SELECT ai_query(:ep, :req) AS raw"
        args = {"ep": endpoint}

    def provider(_question, prompt):
        try:
            row = spark.sql(query, args={**args, "req": prompt}).collect()[0]
        except Exception as exc:  # noqa: BLE001
            # A provider failure is NOT a model behaviour. Its own verdict, so that an
            # outage or a rate limit can never be read as a score.
            return ModelResponse(False,
                                 provider_error=f"{type(exc).__name__}: {str(exc)[:400]}")
        return parse_response(row.raw)

    return provider


print("harness loaded: schema_card, build_prompt, parse_response, grade, run_bank, "
      "find_endpoint, make_model_provider, CALIBRATION_PROVIDERS")
