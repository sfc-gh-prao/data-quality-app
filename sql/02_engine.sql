/* =============================================================================
   Data Quality Monitor — 02_engine.sql
   RUN_DQ_CHECKS: reads active rules from DQ_RULES, compiles each into SQL,
   executes it, and appends results to DQ_RESULTS.

     CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS();                          -- all rules
     CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS('DQ_SAMPLE.RETAIL.ORDERS'); -- one table
     CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS(NULL, 42);                  -- one rule

   Runs with CALLER's rights, so a role can only check tables it can read.
   NOTE: ROW_FILTER and CUSTOM_SQL.failure_condition are raw SQL predicates.
   Restrict write access on DQ_RULES to trusted data-quality admins.
   ============================================================================= */

USE SCHEMA DQ_FRAMEWORK.CORE;

CREATE OR REPLACE PROCEDURE RUN_DQ_CHECKS(
    P_TABLE_FQN    VARCHAR DEFAULT NULL,
    P_RULE_ID      NUMBER  DEFAULT NULL,
    P_TRIGGERED_BY VARCHAR DEFAULT 'MANUAL'
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
COMMENT = 'Data Quality Monitor - executes rules in DQ_RULES and logs to DQ_RESULTS'
EXECUTE AS CALLER
AS
$$
import json
import re
import time
import uuid
from datetime import datetime, timezone

from snowflake.snowpark.types import (
    DoubleType, LongType, StringType, StructField, StructType, TimestampType, TimestampTimeZone,
)

SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
RESULTS_TABLE = "DQ_FRAMEWORK.CORE.DQ_RESULTS"
RULES_TABLE = "DQ_FRAMEWORK.CORE.DQ_RULES"


def q_ident(name):
    """Quote an identifier. Unquoted-style names are upper-cased (Snowflake default)."""
    name = str(name).strip()
    if name.startswith('"') and name.endswith('"'):
        return name
    if SIMPLE_IDENT.match(name):
        name = name.upper()
    return '"' + name.replace('"', '""') + '"'


def q_fqn(fqn):
    parts = [p for p in str(fqn).split(".") if p]
    return ".".join(q_ident(p) for p in parts)


def q_lit(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def build_sql(rule):
    """Return (check_sql, sample_sql). check_sql yields TOTAL_ROWS, FAILED_ROWS."""
    rtype = (rule["RULE_TYPE"] or "").upper()
    params = rule["RULE_PARAMS"] or {}
    if isinstance(params, str):
        params = json.loads(params) if params.strip() else {}
    fqn = ".".join(q_ident(rule[k]) for k in ("DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME"))
    where = f"({rule['ROW_FILTER']})" if rule.get("ROW_FILTER") else "TRUE"
    cols = [c.strip() for c in (rule.get("COLUMN_NAME") or "").split(",") if c.strip()]
    col = q_ident(cols[0]) if cols else None

    def simple(fail_pred):
        check = f"SELECT COUNT(*) AS TOTAL_ROWS, COUNT_IF({fail_pred}) AS FAILED_ROWS FROM {fqn} WHERE {where}"
        sample = f"SELECT * FROM {fqn} WHERE {where} AND ({fail_pred}) LIMIT 100"
        return check, sample

    if rtype in ("NOT_NULL", "ACCEPTED_VALUES", "RANGE", "REGEX", "FRESHNESS", "REFERENTIAL") and not col:
        raise ValueError(f"{rtype} requires COLUMN_NAME")

    if rtype == "NOT_NULL":
        return simple(f"{col} IS NULL")

    if rtype == "UNIQUE":
        if not cols:
            raise ValueError("UNIQUE requires COLUMN_NAME")
        key = ", ".join(q_ident(c) for c in cols)
        check = (
            f"SELECT (SELECT COUNT(*) FROM {fqn} WHERE {where}) AS TOTAL_ROWS, "
            f"(SELECT COALESCE(SUM(N), 0) FROM (SELECT COUNT(*) AS N FROM {fqn} WHERE {where} "
            f"GROUP BY {key} HAVING COUNT(*) > 1)) AS FAILED_ROWS"
        )
        sample = (
            f"SELECT * FROM {fqn} WHERE {where} "
            f"QUALIFY COUNT(*) OVER (PARTITION BY {key}) > 1 ORDER BY {key} LIMIT 100"
        )
        return check, sample

    if rtype == "ACCEPTED_VALUES":
        values = params.get("values") or []
        if not values:
            raise ValueError("ACCEPTED_VALUES requires params.values")
        in_list = ", ".join(q_lit(v) for v in values)
        return simple(f"{col} IS NOT NULL AND {col}::VARCHAR NOT IN ({in_list})")

    if rtype == "RANGE":
        preds = []
        if params.get("min") is not None:
            preds.append(f"{col} < {float(params['min'])}")
        if params.get("max") is not None:
            preds.append(f"{col} > {float(params['max'])}")
        if not preds:
            raise ValueError("RANGE requires params.min and/or params.max")
        return simple(" OR ".join(preds))

    if rtype == "REGEX":
        pattern = params.get("pattern")
        if not pattern:
            raise ValueError("REGEX requires params.pattern")
        return simple(f"{col} IS NOT NULL AND NOT REGEXP_LIKE({col}::VARCHAR, {q_lit(pattern)})")

    if rtype == "FRESHNESS":
        minutes = int(float(params.get("max_age_hours", 24)) * 60)
        check = (
            f"SELECT 1 AS TOTAL_ROWS, IFF(MAX({col}) IS NULL OR "
            f"DATEDIFF('minute', MAX({col})::TIMESTAMP_LTZ, CURRENT_TIMESTAMP()) > {minutes}, 1, 0) AS FAILED_ROWS "
            f"FROM {fqn} WHERE {where}"
        )
        sample = (
            f"SELECT MAX({col}) AS LATEST_VALUE, "
            f"DATEDIFF('hour', MAX({col})::TIMESTAMP_LTZ, CURRENT_TIMESTAMP()) AS AGE_HOURS, "
            f"{minutes / 60} AS MAX_AGE_HOURS FROM {fqn} WHERE {where}"
        )
        return check, sample

    if rtype == "ROW_COUNT":
        preds = []
        if params.get("min") is not None:
            preds.append(f"COUNT(*) < {int(params['min'])}")
        if params.get("max") is not None:
            preds.append(f"COUNT(*) > {int(params['max'])}")
        if not preds:
            raise ValueError("ROW_COUNT requires params.min and/or params.max")
        check = f"SELECT 1 AS TOTAL_ROWS, IFF({' OR '.join(preds)}, 1, 0) AS FAILED_ROWS FROM {fqn} WHERE {where}"
        sample = f"SELECT COUNT(*) AS ROW_COUNT FROM {fqn} WHERE {where}"
        return check, sample

    if rtype == "REFERENTIAL":
        ref_table, ref_col = params.get("ref_table"), params.get("ref_column")
        if not ref_table or not ref_col:
            raise ValueError("REFERENTIAL requires params.ref_table and params.ref_column")
        join = (
            f"FROM {fqn} T LEFT JOIN (SELECT DISTINCT {q_ident(ref_col)} AS DQ__REF FROM {q_fqn(ref_table)}) P "
            f"ON T.{col} = P.DQ__REF WHERE {where}"
        )
        fail = f"T.{col} IS NOT NULL AND P.DQ__REF IS NULL"
        check = f"SELECT COUNT(*) AS TOTAL_ROWS, COUNT_IF({fail}) AS FAILED_ROWS {join}"
        sample = f"SELECT T.* {join} AND {fail} LIMIT 100"
        return check, sample

    if rtype == "CUSTOM_SQL":
        cond = params.get("failure_condition")
        if not cond:
            raise ValueError("CUSTOM_SQL requires params.failure_condition")
        return simple(f"({cond})")

    raise ValueError(f"Unknown RULE_TYPE '{rtype}'")


def run(session, p_table_fqn, p_rule_id, p_triggered_by):
    sql = f"SELECT * FROM {RULES_TABLE} WHERE IS_ACTIVE"
    binds = []
    if p_table_fqn:
        sql += " AND UPPER(DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TABLE_NAME) = UPPER(?)"
        binds.append(p_table_fqn)
    if p_rule_id is not None:
        sql += " AND RULE_ID = ?"
        binds.append(int(p_rule_id))
    rules = [r.as_dict() for r in session.sql(sql + " ORDER BY RULE_ID", params=binds).collect()]

    run_id = str(uuid.uuid4())
    run_ts = datetime.now(timezone.utc)
    rows = []
    for rule in rules:
        started = time.time()
        check_sql = sample_sql = err = None
        total = failed = None
        pct = None
        try:
            check_sql, sample_sql = build_sql(rule)
            res = session.sql(check_sql).collect()[0]
            total, failed = int(res["TOTAL_ROWS"] or 0), int(res["FAILED_ROWS"] or 0)
            pct = round(100.0 * failed / total, 4) if total else 0.0
            status = "FAIL" if pct > float(rule["THRESHOLD_PCT"] or 0) else "PASS"
        except Exception as e:  # keep going; one bad rule must not stop the run
            status, err = "ERROR", str(e)[:4000]
        rows.append((
            run_id, run_ts, rule["RULE_ID"], rule["RULE_NAME"],
            f"{rule['DATABASE_NAME']}.{rule['SCHEMA_NAME']}.{rule['TABLE_NAME']}".upper(),
            rule["COLUMN_NAME"], rule["RULE_TYPE"], rule["DIMENSION"], rule["SEVERITY"],
            total, failed, pct, rule["THRESHOLD_PCT"], status, err,
            int((time.time() - started) * 1000), check_sql, sample_sql, (p_triggered_by or "MANUAL").upper(),
        ))

    s, n, f = StringType(), LongType(), DoubleType()
    schema = StructType([StructField(name, typ) for name, typ in [
        ("RUN_ID", s), ("RUN_TS", TimestampType(TimestampTimeZone.LTZ)), ("RULE_ID", n), ("RULE_NAME", s),
        ("TABLE_FQN", s), ("COLUMN_NAME", s), ("RULE_TYPE", s), ("DIMENSION", s), ("SEVERITY", s),
        ("TOTAL_ROWS", n), ("FAILED_ROWS", n), ("FAILED_PCT", f), ("THRESHOLD_PCT", f), ("STATUS", s),
        ("ERROR_MESSAGE", s), ("DURATION_MS", n), ("CHECK_SQL", s), ("SAMPLE_SQL", s), ("TRIGGERED_BY", s),
    ]])
    if rows:
        rows = [tuple(float(v) if i in (11, 12) and v is not None else v for i, v in enumerate(r)) for r in rows]
        session.create_dataframe(rows, schema=schema).write.mode("append").save_as_table(
            RESULTS_TABLE, column_order="name"
        )

    summary = {s: sum(1 for r in rows if r[13] == s) for s in ("PASS", "FAIL", "ERROR")}
    return {"run_id": run_id, "rules_executed": len(rows), **summary}
$$;

-- -----------------------------------------------------------------------------
-- Schedule: run every check daily at 06:00 UTC. Adjust the CRON as needed.
-- The task owner role needs SELECT on every monitored table.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TASK DQ_DAILY_RUN
  WAREHOUSE = DQ_WH
  SCHEDULE = 'USING CRON 0 6 * * * UTC'
  COMMENT = 'Data Quality Monitor - daily scheduled run'
AS
  CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS(NULL, NULL, 'SCHEDULE');

-- Tasks are created suspended. Resume to enable (requires EXECUTE TASK privilege):
-- ALTER TASK DQ_DAILY_RUN RESUME;

-- -----------------------------------------------------------------------------
-- Optional: email when a CRITICAL/HIGH check fails. Requires a notification
-- integration (CREATE NOTIFICATION INTEGRATION ... TYPE = EMAIL).
-- -----------------------------------------------------------------------------
-- CREATE OR REPLACE ALERT DQ_FAILURE_ALERT
--   WAREHOUSE = DQ_WH
--   SCHEDULE = 'USING CRON 30 6 * * * UTC'
--   IF (EXISTS (
--     SELECT 1 FROM DQ_FRAMEWORK.CORE.V_LATEST_RESULTS
--     WHERE STATUS <> 'PASS' AND SEVERITY IN ('CRITICAL', 'HIGH')
--       AND RUN_TS > DATEADD('hour', -1, CURRENT_TIMESTAMP())))
--   THEN CALL SYSTEM$SEND_EMAIL('MY_EMAIL_INTEGRATION', 'dq-team@example.com',
--          'Data quality failures detected',
--          'One or more CRITICAL/HIGH data quality checks failed. Open the Data Quality Monitor app for details.');
-- ALTER ALERT DQ_FAILURE_ALERT RESUME;
