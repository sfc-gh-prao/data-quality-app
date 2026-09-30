"""Rule compiler shared with the engine.

Mirrors build_sql() in sql/02_engine.sql so the app can preview a rule's result
before saving it. Keep the two in sync when adding rule types.
"""

import json
import re

SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def q_ident(name):
    name = str(name).strip()
    if name.startswith('"') and name.endswith('"'):
        return name
    if SIMPLE_IDENT.match(name):
        name = name.upper()
    return '"' + name.replace('"', '""') + '"'


def q_fqn(fqn):
    return ".".join(q_ident(p) for p in str(fqn).split(".") if p)


def q_lit(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def build_sql(rule):
    """Return (check_sql, sample_sql). check_sql yields TOTAL_ROWS, FAILED_ROWS."""
    rtype = (rule["RULE_TYPE"] or "").upper()
    params = rule.get("RULE_PARAMS") or {}
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
        sample = (f"SELECT * FROM {fqn} WHERE {where} "
                  f"QUALIFY COUNT(*) OVER (PARTITION BY {key}) > 1 ORDER BY {key} LIMIT 100")
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
        check = (f"SELECT 1 AS TOTAL_ROWS, IFF(MAX({col}) IS NULL OR "
                 f"DATEDIFF('minute', MAX({col})::TIMESTAMP_LTZ, CURRENT_TIMESTAMP()) > {minutes}, 1, 0) AS FAILED_ROWS "
                 f"FROM {fqn} WHERE {where}")
        sample = (f"SELECT MAX({col}) AS LATEST_VALUE, "
                  f"DATEDIFF('hour', MAX({col})::TIMESTAMP_LTZ, CURRENT_TIMESTAMP()) AS AGE_HOURS, "
                  f"{minutes / 60} AS MAX_AGE_HOURS FROM {fqn} WHERE {where}")
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
        return check, f"SELECT COUNT(*) AS ROW_COUNT FROM {fqn} WHERE {where}"

    if rtype == "REFERENTIAL":
        ref_table, ref_col = params.get("ref_table"), params.get("ref_column")
        if not ref_table or not ref_col:
            raise ValueError("REFERENTIAL requires params.ref_table and params.ref_column")
        join = (f"FROM {fqn} T LEFT JOIN (SELECT DISTINCT {q_ident(ref_col)} AS DQ__REF FROM {q_fqn(ref_table)}) P "
                f"ON T.{col} = P.DQ__REF WHERE {where}")
        fail = f"T.{col} IS NOT NULL AND P.DQ__REF IS NULL"
        return f"SELECT COUNT(*) AS TOTAL_ROWS, COUNT_IF({fail}) AS FAILED_ROWS {join}", f"SELECT T.* {join} AND {fail} LIMIT 100"

    if rtype == "CUSTOM_SQL":
        cond = params.get("failure_condition")
        if not cond:
            raise ValueError("CUSTOM_SQL requires params.failure_condition")
        return simple(f"({cond})")

    raise ValueError(f"Unknown RULE_TYPE '{rtype}'")
