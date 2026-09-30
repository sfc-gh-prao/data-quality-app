"""Schema Explorer: browse objects, profile columns, get smart rule suggestions."""

import json
import math

import pandas as pd
import streamlit as st

from lib import db, ui
from lib.db import fq, q_ident
from lib.rules_sql import build_sql

db.require_framework()
ui.hero("Schema Explorer", "Browse your data, profile columns, and get smart rule suggestions — preview them, then apply with one click.")
if db.read_only_notice("profile tables and add rules"):
    st.caption("Profiling reads real column values, so Schema Explorer is limited to admins.")
    st.stop()

# ── Constants ────────────────────────────────────────────────────────────────

DIMENSION_FOR = {
    "NOT_NULL": "Completeness", "UNIQUE": "Uniqueness", "ACCEPTED_VALUES": "Validity",
    "RANGE": "Validity", "REGEX": "Validity", "FRESHNESS": "Timeliness",
    "ROW_COUNT": "Volume", "REFERENTIAL": "Consistency", "CUSTOM_SQL": "Consistency",
}
RULE_COLS = ["RULE_NAME", "DESCRIPTION", "DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME", "COLUMN_NAME", "RULE_TYPE",
             "RULE_PARAMS", "ROW_FILTER", "THRESHOLD_PCT", "SEVERITY", "DIMENSION", "OWNER"]
INSERT_SQL = (f"INSERT INTO {fq('DQ_RULES')} ({', '.join(RULE_COLS)}) "
              "SELECT ?, ?, ?, ?, ?, ?, ?, PARSE_JSON(?), ?, ?, ?, ?, ?")

NUMERIC_TYPES = {"NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "DOUBLE", "REAL"}
TIMESTAMP_TYPES = {"TIMESTAMP_LTZ", "TIMESTAMP_NTZ", "TIMESTAMP_TZ", "TIMESTAMP", "DATETIME", "DATE"}
TEXT_TYPES = {"VARCHAR", "TEXT", "STRING", "CHAR", "CHARACTER"}
COMPLEX_TYPES = {"VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY", "VECTOR", "BINARY"}

# Name fragments that imply a column is required / must be non-negative / signals load recency.
REQUIRED_HINTS = ("_ID", "_DATE", "NAME", "STATUS", "AMOUNT", "TYPE")
NON_NEGATIVE_HINTS = ("AMOUNT", "BALANCE", "QTY", "QUANTITY", "PRICE", "BUDGET", "COST", "UNITS", "COUNT")
FRESHNESS_HINTS = ("LOADED", "UPDATED", "MODIFIED", "CREATED", "REFRESHED", "INGEST")

EMAIL_PATTERN = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
PHONE_PATTERN = r"^\+?[0-9\-\(\) ]{7,20}$"

MAX_PROFILE_ROWS = 500_000
MAX_CATEGORICAL = 25        # max distinct values to treat a column as categorical
MAX_PREVIEW_RULES = 60
CONFIRM_OVER_TABLES = 10    # ask before profiling more than this many tables at once

# Columns treated as sensitive: their values (min/max/samples/value lists) are never read or shown.
SENSITIVE_HINTS = ("EMAIL", "PHONE", "MOBILE", "SSN", "SOCIAL_SEC", "TAX_ID", "TIN", "NATIONAL_ID", "PASSPORT",
                   "LICENSE", "DOB", "BIRTH", "ADDRESS", "STREET", "ZIP", "POSTAL", "FIRST_NAME", "LAST_NAME",
                   "FULL_NAME", "CARD", "CC_NUM", "ACCOUNT_NUMBER", "ACCT_NUM", "ROUTING", "IBAN", "IP_ADDR",
                   "PASSWORD", "SECRET", "TOKEN")
SENSITIVE_TAGS = ("SEMANTIC_CATEGORY", "PRIVACY_CATEGORY")


# ── Profiling ────────────────────────────────────────────────────────────────

def _base_type(dtype: str) -> str:
    return str(dtype).split("(")[0].strip().upper()


def _lit(value) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _fqn(database, schema, table) -> str:
    return f"{q_ident(database)}.{q_ident(schema)}.{q_ident(table)}"


def sensitive_columns(database, schema, table, columns: pd.DataFrame) -> dict[str, str]:
    """Columns whose values must not be displayed -> reason. Uses masking policies, classification tags, and names."""
    out = {}
    ent = f"{database}.{schema}.{table}"
    info = f"{q_ident(database)}.INFORMATION_SCHEMA"
    try:
        mp = db.run_uncached(f"SELECT REF_COLUMN_NAME FROM TABLE({info}.POLICY_REFERENCES(REF_ENTITY_NAME => ?, "
                             "REF_ENTITY_DOMAIN => 'table')) WHERE POLICY_KIND = 'MASKING_POLICY'", (ent,))
        out.update({str(c).upper(): "masking policy" for c in mp.get("REF_COLUMN_NAME", [])})
    except Exception:
        pass
    try:
        tags = db.run_uncached(f"SELECT COLUMN_NAME, TAG_NAME, TAG_VALUE FROM TABLE({info}.TAG_REFERENCES_ALL_COLUMNS(?, 'table'))", (ent,))
        for r in tags.itertuples():
            if str(r.TAG_NAME).upper() in SENSITIVE_TAGS and str(r.COLUMN_NAME).upper() not in out:
                out[str(r.COLUMN_NAME).upper()] = f"classified {r.TAG_VALUE}"
    except Exception:
        pass
    for c in columns["COLUMN_NAME"]:
        cu = str(c).upper()
        if cu not in out and any(h in cu for h in SENSITIVE_HINTS):
            out[cu] = "name suggests PII"
    return out


def _build_profile_sql(database, schema, table, columns: pd.DataFrame, row_count: int, sensitive=()) -> str:
    """One UNION ALL query that profiles every column. Large tables are sampled; sensitive values are never read."""
    fqn = _fqn(database, schema, table)
    sample = ""
    if row_count and row_count > MAX_PROFILE_ROWS:
        pct = max(0.1, math.ceil(100.0 * MAX_PROFILE_ROWS / row_count * 10) / 10)
        sample = f" TABLESAMPLE ({pct})"
    src = f"(SELECT * FROM {fqn}{sample})"
    parts = []
    for _, row in columns.iterrows():
        col = q_ident(row["COLUMN_NAME"])
        bt = _base_type(row["DATA_TYPE"])
        simple = bt not in COMPLEX_TYPES and str(row["COLUMN_NAME"]).upper() not in sensitive
        mn = f"MIN({col})::VARCHAR" if simple else "NULL::VARCHAR"
        mx = f"MAX({col})::VARCHAR" if simple else "NULL::VARCHAR"
        age = (f"DATEDIFF('minute', MAX({col})::TIMESTAMP_LTZ, CURRENT_TIMESTAMP()) / 60.0"
               if bt in TIMESTAMP_TYPES else "NULL::FLOAT")
        samples = (f"ARRAY_SLICE((SELECT ARRAY_AGG(DISTINCT V) WITHIN GROUP (ORDER BY V) FROM "
                   f"(SELECT {col}::VARCHAR AS V FROM {fqn} WHERE {col} IS NOT NULL LIMIT 1000)), 0, 5)"
                   if simple else "NULL::ARRAY")
        parts.append(
            f"SELECT {_lit(row['COLUMN_NAME'])} AS COLUMN_NAME, {_lit(row['DATA_TYPE'])} AS DATA_TYPE, "
            f"COUNT(*) AS TOTAL_ROWS, COUNT_IF({col} IS NULL) AS NULL_COUNT, "
            f"COUNT(DISTINCT {col}) AS DISTINCT_COUNT, {mn} AS MIN_VALUE, {mx} AS MAX_VALUE, "
            f"{age} AS AGE_HOURS, {samples} AS SAMPLE_VALUES FROM {src}"
        )
    return " UNION ALL ".join(parts)


def _as_list(v) -> list:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _signals(p) -> str:
    bt = _base_type(p["DATA_TYPE"])
    tags = []
    if p.get("SENSITIVE"):
        tags.append(f"sensitive ({p['SENSITIVE']})")
    if str(p["COLUMN_NAME"]).upper().endswith("_ID") and p["UNIQUENESS_PCT"] >= 99:
        tags.append("PK?")
    elif str(p["COLUMN_NAME"]).upper().endswith("_ID"):
        tags.append("FK?")
    if p["VALUES_LIST"]:
        tags.append("categorical")
    if bt in TIMESTAMP_TYPES:
        tags.append("timestamp")
    if p["NULL_PCT"] > 5:
        tags.append("high nulls")
    if bt in NUMERIC_TYPES and p["MIN_VALUE"] is not None:
        try:
            if float(p["MIN_VALUE"]) < 0:
                tags.append("negatives")
        except (TypeError, ValueError):
            pass
    return ", ".join(tags)


def profile_table(database, schema, table, columns: pd.DataFrame, row_count: int) -> pd.DataFrame:
    """Profile a table. Categorical value lists are read from the FULL table, not the sample.
    Sensitive columns only get counts (nulls, distinct) — their values are never selected."""
    fqn = _fqn(database, schema, table)
    sensitive = sensitive_columns(database, schema, table, columns)
    df = db.run_uncached(_build_profile_sql(database, schema, table, columns, row_count, sensitive))
    df["SENSITIVE"] = df["COLUMN_NAME"].str.upper().map(sensitive)
    total = df["TOTAL_ROWS"].replace(0, 1)
    df["NULL_PCT"] = (100.0 * df["NULL_COUNT"] / total).round(2)
    df["UNIQUENESS_PCT"] = (100.0 * df["DISTINCT_COUNT"] / total).round(2)
    df["AGE_HOURS"] = pd.to_numeric(df["AGE_HOURS"], errors="coerce")

    values = []
    for _, p in df.iterrows():
        bt = _base_type(p["DATA_TYPE"])
        if (0 < int(p["DISTINCT_COUNT"] or 0) <= MAX_CATEGORICAL and bt in TEXT_TYPES | NUMERIC_TYPES
                and not p["SENSITIVE"]):
            col = q_ident(p["COLUMN_NAME"])
            v = db.run_uncached(f"SELECT DISTINCT {col}::VARCHAR AS V FROM {fqn} "
                                f"WHERE {col} IS NOT NULL ORDER BY 1 LIMIT {MAX_CATEGORICAL + 1}")
            vals = v["V"].tolist() if not v.empty else []
            values.append(vals if 0 < len(vals) <= MAX_CATEGORICAL else None)
        else:
            values.append(None)
    df["VALUES_LIST"] = values
    df["SAMPLE_VALUES"] = df["SAMPLE_VALUES"].apply(lambda v: ", ".join(map(str, _as_list(v))))
    df["SIGNALS"] = df.apply(_signals, axis=1)
    return df


# ── Suggestion engine ────────────────────────────────────────────────────────

def _find_parent(col_name: str, schema_columns: pd.DataFrame, current_table: str):
    """Match FK columns like MEMBER_ID to another table in the schema that has the same column."""
    entity = col_name.upper()[:-3]
    others = schema_columns[(schema_columns["TABLE_NAME"] != current_table) &
                            (schema_columns["COLUMN_NAME"].str.upper() == col_name.upper())]
    # Prefer a table whose own first column is this key (i.e. where it is the PK).
    firsts = schema_columns.groupby("TABLE_NAME").head(1)
    pk_tables = set(firsts[firsts["COLUMN_NAME"].str.upper() == col_name.upper()]["TABLE_NAME"])
    candidates = list(others["TABLE_NAME"].unique())
    for tbl in candidates:
        if tbl in pk_tables:
            return tbl, "high" if entity in tbl.upper() else "medium"
    for tbl in candidates:
        if entity in tbl.upper():
            return tbl, "medium"
    return None


def suggest_rules(database, schema, table, profile: pd.DataFrame, existing: pd.DataFrame,
                  schema_columns: pd.DataFrame, row_count: int) -> list[dict]:
    covered = set()
    if not existing.empty:
        covered = {(str(r["COLUMN_NAME"] or "").upper(), str(r["RULE_TYPE"]).upper()) for _, r in existing.iterrows()}
    out = []

    def add(col, rtype, name, desc, params, severity, threshold, confidence, reason):
        if (str(col or "").upper(), rtype) in covered:
            return
        out.append({"table_name": table, "column_name": col, "rule_type": rtype, "rule_name": name,
                    "description": desc, "rule_params": params, "severity": severity,
                    "threshold_pct": float(threshold), "confidence": confidence, "reason": reason})

    for pos, p in profile.reset_index(drop=True).iterrows():
        col = p["COLUMN_NAME"]
        cu = str(col).upper()
        bt = _base_type(p["DATA_TYPE"])
        total = int(p["TOTAL_ROWS"] or 0)
        null_pct, uniq_pct = float(p["NULL_PCT"]), float(p["UNIQUENESS_PCT"])
        is_pk = cu.endswith("_ID") and pos == 0

        if is_pk:
            add(col, "NOT_NULL", f"{table} {col} present", f"Primary key {col} must not be null", {},
                "CRITICAL", 0, "high", f"First column, {null_pct}% nulls")
            add(col, "UNIQUE", f"{table} {col} unique", f"Primary key {col} must not repeat", {},
                "CRITICAL", 0, "high" if uniq_pct >= 99 else "medium",
                f"{uniq_pct}% unique" + (" — likely PK" if uniq_pct >= 99 else " — duplicates present"))
        elif cu.endswith("_ID"):
            parent = _find_parent(col, schema_columns, table)
            if parent:
                ptbl, conf = parent
                add(col, "REFERENTIAL", f"{table} {col} references {ptbl}", f"{col} must exist in {ptbl}",
                    {"ref_table": f"{database}.{schema}.{ptbl}", "ref_column": col},
                    "HIGH", 0.1, conf, f"Matches key column in {ptbl}")

        if not is_pk and total > 100 and null_pct == 0:
            required = any(h in cu for h in REQUIRED_HINTS)
            add(col, "NOT_NULL", f"{table} {col} populated", f"{col} should not be null", {},
                "HIGH" if required else "MEDIUM", 0.5, "medium" if required else "low",
                "0% nulls" + (" and name implies required" if required else ""))

        vals = p["VALUES_LIST"]
        if vals and not cu.endswith("_ID") and bt in TEXT_TYPES:
            add(col, "ACCEPTED_VALUES", f"{table} {col} valid values", f"{col} must be one of the known values",
                {"values": [str(v) for v in vals]}, "MEDIUM", 0, "medium",
                f"{len(vals)} distinct values (full table): {', '.join(map(str, vals[:6]))}{'…' if len(vals) > 6 else ''}")

        if bt in NUMERIC_TYPES and p["MIN_VALUE"] is not None and not cu.endswith("_ID"):
            try:
                mn = float(p["MIN_VALUE"])
            except (TypeError, ValueError):
                mn = None
            if mn is not None and mn < 0:
                hinted = any(h in cu for h in NON_NEGATIVE_HINTS)
                add(col, "RANGE", f"{table} {col} non-negative", f"{col} should be >= 0", {"min": 0},
                    "HIGH" if hinted else "MEDIUM", 1, "medium" if hinted else "low",
                    f"Min value {mn:g}" + (" — name implies non-negative" if hinted else ""))

        if bt in TIMESTAMP_TYPES and any(h in cu for h in FRESHNESS_HINTS):
            hours = 24
            age = p["AGE_HOURS"]
            if pd.notna(age):
                verdict = "would FAIL" if age > hours else "would PASS"
                reason = f"Latest value {age:,.1f}h old — {verdict} at {hours}h"
            else:
                reason = "Load timestamp column"
            add(col, "FRESHNESS", f"{table} {col} freshness", f"{col} should be refreshed within {hours}h",
                {"max_age_hours": hours}, "CRITICAL", 0, "high", reason)

        if bt in TEXT_TYPES and "EMAIL" in cu:
            add(col, "REGEX", f"{table} {col} format", f"{col} must be a valid email address",
                {"pattern": EMAIL_PATTERN}, "MEDIUM", 2, "medium", "Column name indicates email")
        if bt in TEXT_TYPES and "PHONE" in cu:
            add(col, "REGEX", f"{table} {col} format", f"{col} must match a phone format",
                {"pattern": PHONE_PATTERN}, "LOW", 5, "medium", "Column name indicates phone")

    if row_count:
        add(None, "ROW_COUNT", f"{table} row count", "Row count within expected bounds",
            {"min": max(1, int(row_count * 0.8)), "max": int(row_count * 2)}, "MEDIUM", 0, "medium",
            f"Current {row_count:,} rows; bounds 80%–200%")
    return out


# ── Suggestion review / preview / apply (shared by single + batch views) ─────

CONF_SETS = {"high": {"high"}, "medium": {"high", "medium"}, "all": {"high", "medium", "low"}, "none": set()}


def render_suggestions(suggestions: list[dict], scope: str, database: str, schema: str):
    if not suggestions:
        st.success("Nothing to suggest — existing rules already cover what the profile detected.")
        return

    mode_key, ver_key = f"sugmode_{scope}", f"sugver_{scope}"
    mode = st.session_state.get(mode_key, "high")
    qc = st.columns([1, 1, 1, 1, 3])
    for c, (label, m) in zip(qc, [("High only", "high"), ("High + medium", "medium"), ("All", "all"), ("Clear", "none")]):
        if c.button(label, key=f"{scope}_sel_{m}", type="primary" if m == mode else "secondary", use_container_width=True):
            st.session_state[mode_key] = m
            st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
            st.rerun()

    sug_df = pd.DataFrame(suggestions)
    sug_df.insert(0, "APPLY", sug_df["confidence"].isin(CONF_SETS[mode]))
    sug_df["rule_params_json"] = sug_df["rule_params"].apply(json.dumps)

    view = sug_df
    if sug_df["table_name"].nunique() > 1:
        fc = st.columns(2)
        tf = fc[0].multiselect("Filter tables", sorted(sug_df["table_name"].unique()), placeholder="All tables", key=f"{scope}_tf")
        cf = fc[1].multiselect("Filter confidence", ["high", "medium", "low"], placeholder="All", key=f"{scope}_cf")
        if tf:
            view = view[view["table_name"].isin(tf)]
        if cf:
            view = view[view["confidence"].isin(cf)]

    shown = ["APPLY", "severity", "table_name", "column_name", "rule_type", "rule_name", "threshold_pct", "confidence", "reason"]
    edited = st.data_editor(
        view[shown], use_container_width=True, hide_index=True, key=f"{scope}_ed_{st.session_state.get(ver_key, 0)}",
        height=min(520, 40 + 35 * len(view)),
        column_config={
            "APPLY": st.column_config.CheckboxColumn("Apply"),
            "severity": st.column_config.SelectboxColumn("Severity", options=ui.SEVERITY_ORDER, required=True),
            "table_name": "Table", "column_name": "Column", "rule_type": "Type", "rule_name": "Rule name",
            "threshold_pct": st.column_config.NumberColumn("Threshold %", min_value=0.0, max_value=100.0, step=0.1),
            "confidence": "Confidence", "reason": st.column_config.TextColumn("Why", width="large"),
        },
        disabled=["table_name", "column_name", "rule_type", "confidence", "reason"],
    )
    selected = edited[edited["APPLY"]]
    st.caption(f"{len(selected)} of {len(sug_df)} selected. Low-confidence suggestions start unchecked — review before applying.")

    with st.container(border=True):
        ui.section("Apply settings")
        bc = st.columns([1.2, 2, 1.2])
        owner = bc[0].text_input("Owner (all selected)", placeholder="e.g. Data Engineering", key=f"{scope}_owner")
        row_filter = bc[1].text_input("Row filter (all selected, optional)", placeholder="STATUS <> 'CANCELLED'", key=f"{scope}_rf")
        run_after = bc[2].checkbox("Run checks after applying", value=True, key=f"{scope}_run")

        def rule_rows():
            for idx, ed in selected.iterrows():
                s = sug_df.loc[idx]
                yield idx, {
                    "RULE_NAME": ed["rule_name"], "DESCRIPTION": s["description"], "DATABASE_NAME": database,
                    "SCHEMA_NAME": schema, "TABLE_NAME": s["table_name"], "COLUMN_NAME": s["column_name"],
                    "RULE_TYPE": s["rule_type"], "RULE_PARAMS": s["rule_params_json"],
                    "ROW_FILTER": row_filter or None, "THRESHOLD_PCT": float(ed["threshold_pct"]),
                    "SEVERITY": ed["severity"], "DIMENSION": DIMENSION_FOR.get(s["rule_type"], "Validity"),
                    "OWNER": owner or None,
                }

        ac = st.columns([1, 1, 3])
        if ac[0].button("Preview results", icon=":material/visibility:", key=f"{scope}_prev", disabled=selected.empty):
            rows = list(rule_rows())
            if len(rows) > MAX_PREVIEW_RULES:
                st.warning(f"Previewing the first {MAX_PREVIEW_RULES} of {len(rows)} selected rules.")
                rows = rows[:MAX_PREVIEW_RULES]
            results = []
            bar = st.progress(0.0, text="Previewing...")
            for i, (_, r) in enumerate(rows):
                bar.progress((i + 1) / len(rows), text=f"Previewing {r['RULE_NAME']}")
                try:
                    check, _ = build_sql(r)
                    res = db.run_uncached(check).iloc[0]
                    total, failed = int(res["TOTAL_ROWS"] or 0), int(res["FAILED_ROWS"] or 0)
                    pct = round(100.0 * failed / total, 2) if total else 0.0
                    status = "FAIL" if pct > r["THRESHOLD_PCT"] else "PASS"
                    results.append({"STATUS": status, "RULE": r["RULE_NAME"], "TABLE": r["TABLE_NAME"],
                                    "FAILED_PCT": pct, "THRESHOLD_PCT": r["THRESHOLD_PCT"], "FAILED_ROWS": failed, "ERROR": None})
                except Exception as e:
                    results.append({"STATUS": "ERROR", "RULE": r["RULE_NAME"], "TABLE": r["TABLE_NAME"],
                                    "FAILED_PCT": None, "THRESHOLD_PCT": r["THRESHOLD_PCT"], "FAILED_ROWS": None, "ERROR": str(e)[:300]})
            bar.empty()
            st.session_state[f"{scope}_preview"] = pd.DataFrame(results)

        if ac[1].button(f"Apply {len(selected)} rules", icon=":material/add_circle:", type="primary",
                        key=f"{scope}_apply", disabled=selected.empty):
            rows = [r for _, r in rule_rows()]
            db.execute_many(INSERT_SQL, [tuple(r[c] for c in RULE_COLS) for r in rows])
            touched = sorted({r["TABLE_NAME"] for r in rows})
            msg = f"Applied {len(rows)} rules across {len(touched)} table(s)."
            if run_after:
                totals = {"PASS": 0, "FAIL": 0, "ERROR": 0}
                with st.spinner(f"Running checks on {len(touched)} table(s)..."):
                    for t in touched:
                        r = db.run_checks(table_fqn=f"{database}.{schema}.{t}")
                        for k in totals:
                            totals[k] += r.get(k, 0)
                msg += f" First run: {totals['PASS']} pass · {totals['FAIL']} fail · {totals['ERROR']} error."
            st.session_state["explorer_flash"] = msg
            st.session_state.pop(f"{scope}_preview", None)
            st.rerun()

    prev = st.session_state.get(f"{scope}_preview")
    if prev is not None and not prev.empty:
        pc = st.columns(3)
        pc[0].metric("Would pass", int((prev["STATUS"] == "PASS").sum()))
        pc[1].metric("Would fail", int((prev["STATUS"] == "FAIL").sum()))
        pc[2].metric("Errors", int((prev["STATUS"] == "ERROR").sum()))
        st.dataframe(prev, use_container_width=True, hide_index=True,
                     column_config={"FAILED_PCT": st.column_config.NumberColumn("Failed %", format="%.2f"),
                                    "THRESHOLD_PCT": st.column_config.NumberColumn("Threshold %", format="%.2f"),
                                    "FAILED_ROWS": st.column_config.NumberColumn("Failed rows", format="%d")})
        st.caption("Preview runs each check once without saving. Adjust thresholds above if a rule would fail on known-good data.")


def render_profile(pdf: pd.DataFrame, covered_cols: set):
    show = pdf.copy()
    show["HAS_RULE"] = show["COLUMN_NAME"].str.upper().isin(covered_cols)
    st.dataframe(
        show[["COLUMN_NAME", "DATA_TYPE", "SIGNALS", "HAS_RULE", "NULL_PCT", "DISTINCT_COUNT", "UNIQUENESS_PCT",
              "MIN_VALUE", "MAX_VALUE", "SAMPLE_VALUES"]],
        use_container_width=True, hide_index=True, height=min(460, 40 + 35 * len(show)),
        column_config={
            "COLUMN_NAME": "Column", "DATA_TYPE": "Type", "SIGNALS": "Signals",
            "HAS_RULE": st.column_config.CheckboxColumn("Has rule"),
            "NULL_PCT": st.column_config.ProgressColumn("Null %", min_value=0, max_value=100, format="%.1f%%"),
            "DISTINCT_COUNT": st.column_config.NumberColumn("Distinct", format="%d"),
            "UNIQUENESS_PCT": st.column_config.NumberColumn("Unique %", format="%.1f%%"),
            "MIN_VALUE": "Min", "MAX_VALUE": "Max", "SAMPLE_VALUES": "Sample values",
        },
    )


# ── Page ─────────────────────────────────────────────────────────────────────

if msg := st.session_state.pop("explorer_flash", None):
    st.success(msg)

ui.section("1. Browse")
bc = st.columns([1.5, 1.5, 2.5])
dbs = db.query("SHOW TERSE DATABASES")
db_names = sorted(dbs["NAME"].tolist()) if "NAME" in dbs.columns else []
database = bc[0].selectbox("Database", db_names, index=None, placeholder="Select database", key="exp_db")
schema = None
if database:
    schemas = db.query(f"SELECT SCHEMA_NAME FROM {q_ident(database)}.INFORMATION_SCHEMA.SCHEMATA "
                       "WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' ORDER BY 1")
    schema = bc[1].selectbox("Schema", schemas["SCHEMA_NAME"].tolist(), index=None, placeholder="Select schema", key="exp_schema")

if not (database and schema):
    st.caption("Pick a database and schema to start.")
    st.stop()

info = f"{q_ident(database)}.INFORMATION_SCHEMA"
tables_df = db.query(
    f"""WITH C AS (SELECT TABLE_NAME, COUNT(*) AS N_COLS FROM {info}.COLUMNS WHERE TABLE_SCHEMA = ? GROUP BY 1),
            R AS (SELECT TABLE_NAME, COUNT(*) AS RULE_COUNT, COUNT(DISTINCT UPPER(COLUMN_NAME)) AS COVERED_COLS
                  FROM {fq('DQ_RULES')} WHERE DATABASE_NAME = ? AND SCHEMA_NAME = ? AND IS_ACTIVE GROUP BY 1),
            H AS (SELECT SPLIT_PART(TABLE_FQN, '.', 3) AS TABLE_NAME, DQ_SCORE
                  FROM {fq('V_TABLE_HEALTH')} WHERE TABLE_FQN LIKE ?)
        SELECT T.TABLE_NAME, T.TABLE_TYPE, T.ROW_COUNT, ROUND(T.BYTES / 1024 / 1024, 1) AS SIZE_MB,
               COALESCE(R.RULE_COUNT, 0) AS RULE_COUNT, C.N_COLS,
               ROUND(100 * COALESCE(R.COVERED_COLS, 0) / NULLIF(C.N_COLS, 0), 0) AS COVERAGE_PCT,
               H.DQ_SCORE, T.LAST_ALTERED
        FROM {info}.TABLES T
        LEFT JOIN C ON C.TABLE_NAME = T.TABLE_NAME
        LEFT JOIN R ON R.TABLE_NAME = T.TABLE_NAME
        LEFT JOIN H ON H.TABLE_NAME = T.TABLE_NAME
        WHERE T.TABLE_SCHEMA = ? AND T.TABLE_TYPE IN ('BASE TABLE', 'VIEW', 'DYNAMIC TABLE')
        ORDER BY COVERAGE_PCT ASC NULLS FIRST, T.ROW_COUNT DESC NULLS LAST""",
    (schema, database, schema, f"{database}.{schema}.%".upper(), schema),
)
if tables_df.empty:
    st.info("No tables found in this schema.")
    st.stop()

def fmt_table(t):
    r = tables_df.set_index("TABLE_NAME").loc[t]
    rc = f"{int(r['ROW_COUNT']):,}" if pd.notna(r["ROW_COUNT"]) else "?"
    return f"{t}  ·  {rc} rows" + (f"  ·  {int(r['RULE_COUNT'])} rules" if r["RULE_COUNT"] else "")

selected = bc[2].multiselect("Tables", tables_df["TABLE_NAME"].tolist(), format_func=fmt_table,
                             placeholder="Pick one to deep-dive, several to batch — or none for all", key="exp_tables")

schema_cols = db.query(f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM {info}.COLUMNS WHERE TABLE_SCHEMA = ? "
                       "ORDER BY TABLE_NAME, ORDINAL_POSITION", (schema,))
schema_rules = db.query(
    f"""SELECT r.RULE_ID, r.TABLE_NAME, r.RULE_NAME, r.COLUMN_NAME, r.RULE_TYPE, r.SEVERITY, r.IS_ACTIVE,
               l.STATUS AS LAST_STATUS, l.FAILED_PCT, l.RUN_TS
        FROM {fq('DQ_RULES')} r LEFT JOIN {fq('V_LATEST_RESULTS')} l ON l.RULE_ID = r.RULE_ID
        WHERE r.DATABASE_NAME = ? AND r.SCHEMA_NAME = ? ORDER BY r.TABLE_NAME, r.RULE_ID""",
    (database, schema),
)
profiles = st.session_state.setdefault("profiles", {})


def pkey(t):
    return f"{database}.{schema}.{t}"


def rules_for(t, active_only=True):
    df = schema_rules[schema_rules["TABLE_NAME"] == t]
    return df[df["IS_ACTIVE"]] if active_only and not df.empty else df


def covered_for(t):
    r = rules_for(t)
    return set(r["COLUMN_NAME"].dropna().str.upper()) if not r.empty else set()


def row_count_for(t):
    v = tables_df.set_index("TABLE_NAME").loc[t, "ROW_COUNT"]
    return int(v) if pd.notna(v) else 0


def run_profile(t):
    cols = schema_cols[schema_cols["TABLE_NAME"] == t].reset_index(drop=True)
    profiles[pkey(t)] = profile_table(database, schema, t, cols, row_count_for(t))


def suggestions_for(t):
    return suggest_rules(database, schema, t, profiles[pkey(t)], rules_for(t), schema_cols, row_count_for(t))


# Schema overview
with st.expander(f"{len(tables_df)} objects in {database}.{schema} — least-covered first", expanded=len(selected) != 1):
    st.dataframe(
        tables_df, use_container_width=True, hide_index=True, height=min(380, 40 + 35 * len(tables_df)),
        column_config={
            "TABLE_NAME": "Table", "TABLE_TYPE": "Type",
            "ROW_COUNT": st.column_config.NumberColumn("Rows", format="%d"),
            "SIZE_MB": st.column_config.NumberColumn("Size MB", format="%.1f"),
            "RULE_COUNT": st.column_config.NumberColumn("Rules", format="%d"),
            "N_COLS": st.column_config.NumberColumn("Columns", format="%d"),
            "COVERAGE_PCT": st.column_config.ProgressColumn("Column coverage", min_value=0, max_value=100, format="%d%%"),
            "DQ_SCORE": st.column_config.NumberColumn("DQ score", format="%.1f"),
            "LAST_ALTERED": st.column_config.DatetimeColumn("Last altered", format="MMM DD, HH:mm"),
        },
    )

# ── Single-table deep dive ───────────────────────────────────────────────────
if len(selected) == 1:
    table = selected[0]
    r = tables_df.set_index("TABLE_NAME").loc[table]
    covered = covered_for(table)
    hc = st.columns(5)
    hc[0].metric("Rows", f"{int(r['ROW_COUNT']):,}" if pd.notna(r["ROW_COUNT"]) else "—")
    hc[1].metric("Columns", int(r["N_COLS"] or 0))
    hc[2].metric("Active rules", int(r["RULE_COUNT"]))
    hc[3].metric("Column coverage", f"{len(covered)}/{int(r['N_COLS'] or 0)}")
    hc[4].metric("DQ score", f"{r['DQ_SCORE']:.1f}" if pd.notna(r["DQ_SCORE"]) else "—")

    ui.section("2. Profile & suggest")
    if st.button("Profile this table" if pkey(table) not in profiles else "Re-profile", icon=":material/analytics:",
                 type="primary", key="btn_profile_one"):
        with st.spinner(f"Profiling {pkey(table)}..."):
            try:
                run_profile(table)
            except Exception as e:
                st.error(f"Profiling failed: {e}")

    t_prof, t_sug, t_rules = st.tabs([":material/analytics: Profile", ":material/lightbulb: Suggestions",
                                      ":material/rule: Existing rules"])
    with t_prof:
        if pkey(table) in profiles:
            render_profile(profiles[pkey(table)], covered)
        else:
            st.caption("Click **Profile this table** to compute column statistics.")
    with t_sug:
        if pkey(table) in profiles:
            render_suggestions(suggestions_for(table), f"one_{pkey(table)}", database, schema)
        else:
            st.caption("Suggestions appear after profiling.")
    with t_rules:
        er = rules_for(table, active_only=False)
        if er.empty:
            st.caption("No rules on this table yet.")
        else:
            st.dataframe(er.drop(columns=["TABLE_NAME"]), use_container_width=True, hide_index=True,
                         column_config={"IS_ACTIVE": st.column_config.CheckboxColumn("Active"),
                                        "FAILED_PCT": st.column_config.NumberColumn("Failed %", format="%.2f"),
                                        "RUN_TS": st.column_config.DatetimeColumn("Last run", format="MMM DD, HH:mm")})
            st.caption("Edit or delete rules on the **Rules** page.")

# ── Batch profile ────────────────────────────────────────────────────────────
else:
    targets = tables_df[tables_df["TABLE_NAME"].isin(selected)] if selected else tables_df[tables_df["ROW_COUNT"].fillna(0) > 0]
    names = targets["TABLE_NAME"].tolist()
    ui.section("2. Profile tables")
    already = sum(1 for t in names if pkey(t) in profiles)
    st.caption(f"{len(names)} table(s) targeted ({already} already profiled this session). "
               f"Tables over {MAX_PROFILE_ROWS:,} rows are sampled; categorical value lists always use the full table.")
    bcol = st.columns([1.3, 1.3, 3])
    run_missing = bcol[0].button(f"Profile {len(names) - already} new" if already else f"Profile {len(names)} tables",
                                 icon=":material/analytics:", type="primary", key="btn_profile_batch",
                                 disabled=not names or already == len(names))
    run_all = bcol[1].button("Re-profile all", key="btn_reprofile", disabled=not already)
    if run_missing or run_all:
        todo = names if run_all else [t for t in names if pkey(t) not in profiles]
        st.session_state["pending_profile"] = todo
    todo = st.session_state.get("pending_profile")
    if todo and len(todo) > CONFIRM_OVER_TABLES and not st.session_state.get("profile_confirmed"):
        big = targets[targets["TABLE_NAME"].isin(todo)]
        rows = int(big["ROW_COUNT"].fillna(0).sum())
        with st.container(border=True):
            st.warning(f"This will profile **{len(todo)} tables** (~{rows:,} rows total, about {int(big['N_COLS'].fillna(0).sum())} "
                       f"columns) on the app's warehouse. Tables over {MAX_PROFILE_ROWS:,} rows are sampled, but low-cardinality "
                       "columns are scanned in full.", icon=":material/warning:")
            cc = st.columns([1, 1, 4])
            if cc[0].button("Profile anyway", type="primary", key="profile_confirm"):
                st.session_state["profile_confirmed"] = True
                st.rerun()
            if cc[1].button("Cancel", key="profile_cancel"):
                st.session_state.pop("pending_profile", None)
                st.rerun()
    elif todo:
        st.session_state.pop("pending_profile", None)
        st.session_state.pop("profile_confirmed", None)
        bar = st.progress(0.0, text="Starting...")
        for i, t in enumerate(todo):
            bar.progress((i + 1) / len(todo), text=f"Profiling {t} ({i + 1}/{len(todo)})")
            try:
                run_profile(t)
            except Exception as e:
                st.warning(f"Failed to profile {t}: {e}")
        bar.empty()

    done = [t for t in names if pkey(t) in profiles]
    if done:
        all_sugs = [s for t in done for s in suggestions_for(t)]
        sc = st.columns(4)
        sc[0].metric("Tables profiled", len(done))
        sc[1].metric("Columns", sum(len(profiles[pkey(t)]) for t in done))
        sc[2].metric("High-null columns (>5%)", sum(int((profiles[pkey(t)]["NULL_PCT"] > 5).sum()) for t in done))
        sc[3].metric("Suggestions", len(all_sugs))

        with st.expander("Column profiles by table"):
            for t in done:
                st.markdown(f"**{t}**")
                render_profile(profiles[pkey(t)], covered_for(t))

        st.divider()
        ui.section("3. Suggested rules")
        render_suggestions(all_sugs, f"batch_{database}.{schema}", database, schema)
