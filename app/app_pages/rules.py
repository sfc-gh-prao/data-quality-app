"""Rules: manage the rule catalog — edit, add, import/export, run."""

import json

import pandas as pd
import streamlit as st

from lib import db, ui
from lib.db import fq, q_ident

db.require_framework()
ui.hero("Rules", "Define what 'good data' means. Rules are configuration — no code changes needed to monitor a new table.")

RULE_TYPES = {
    "NOT_NULL": ("Completeness", "Column must not be NULL"),
    "UNIQUE": ("Uniqueness", "Column (or column combination) must not repeat"),
    "ACCEPTED_VALUES": ("Validity", "Column must be one of a list of values"),
    "RANGE": ("Validity", "Numeric/date column within min/max"),
    "REGEX": ("Validity", "Text column must match a regular expression"),
    "FRESHNESS": ("Timeliness", "Newest timestamp must be recent"),
    "ROW_COUNT": ("Volume", "Table row count within bounds"),
    "REFERENTIAL": ("Consistency", "Every value must exist in a parent table"),
    "CUSTOM_SQL": ("Consistency", "Any SQL predicate that identifies bad rows"),
}
REGEX_PRESETS = {
    "Custom": "",
    "Email": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    "US phone (+1-NNN-NNN-NNNN)": r"^\+1-[0-9]{3}-[0-9]{3}-[0-9]{4}$",
    "US ZIP": r"^[0-9]{5}(-[0-9]{4})?$",
    "UUID": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "Alphanumeric code": r"^[A-Z0-9_-]+$",
}
RULE_COLS = ["RULE_NAME", "DESCRIPTION", "DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME", "COLUMN_NAME", "RULE_TYPE",
             "RULE_PARAMS", "ROW_FILTER", "THRESHOLD_PCT", "SEVERITY", "DIMENSION", "OWNER"]
INSERT_SQL = (
    f"INSERT INTO {fq('DQ_RULES')} ({', '.join(RULE_COLS)}) "
    "SELECT %s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s, %s, %s"
)

rules = db.query(
    f"""SELECT r.RULE_ID, r.IS_ACTIVE, r.RULE_NAME, r.DATABASE_NAME || '.' || r.SCHEMA_NAME || '.' || r.TABLE_NAME AS TABLE_FQN,
               r.COLUMN_NAME, r.RULE_TYPE, TO_JSON(r.RULE_PARAMS) AS RULE_PARAMS, r.ROW_FILTER, r.THRESHOLD_PCT,
               r.SEVERITY, r.DIMENSION, r.OWNER, r.DESCRIPTION, l.STATUS AS LAST_STATUS, r.UPDATED_AT,
               r.DATABASE_NAME, r.SCHEMA_NAME, r.TABLE_NAME
        FROM {fq('DQ_RULES')} r
        LEFT JOIN {fq('V_LATEST_RESULTS')} l ON l.RULE_ID = r.RULE_ID
        ORDER BY TABLE_FQN, r.RULE_ID"""
)

tab_cat, tab_new, tab_io = st.tabs([":material/list: Catalog", ":material/add_circle: New rule", ":material/swap_vert: Import / Export"])

# ---- Catalog -------------------------------------------------------------------
with tab_cat:
    c = st.columns(4)
    with c[0]:
        ui.kpi("Rules", len(rules), f"{int(rules['IS_ACTIVE'].sum()) if len(rules) else 0} active", ui.NAVY)
    with c[1]:
        ui.kpi("Tables covered", rules["TABLE_FQN"].nunique(), "with at least one rule", ui.PRIMARY)
    with c[2]:
        ui.kpi("Rule types used", rules["RULE_TYPE"].nunique(), f"of {len(RULE_TYPES)} available", ui.PRIMARY)
    with c[3]:
        ui.kpi("Owners", rules["OWNER"].nunique(), "teams accountable", ui.PRIMARY)
    st.write("")

    fc = st.columns([2, 1.5, 2])
    t_filter = fc[0].multiselect("Filter tables", sorted(rules["TABLE_FQN"].unique()), placeholder="All tables")
    ty_filter = fc[1].multiselect("Rule type", list(RULE_TYPES), placeholder="All types")
    text = fc[2].text_input("Search", placeholder="rule name, column, owner...")
    view = rules.copy()
    if t_filter:
        view = view[view["TABLE_FQN"].isin(t_filter)]
    if ty_filter:
        view = view[view["RULE_TYPE"].isin(ty_filter)]
    if text:
        blob = view[["RULE_NAME", "COLUMN_NAME", "OWNER", "DESCRIPTION"]].fillna("").astype(str).agg(" ".join, axis=1)
        view = view[blob.str.contains(text, case=False, regex=False)]

    editable = ["IS_ACTIVE", "THRESHOLD_PCT", "SEVERITY", "OWNER"]
    shown = ["RULE_ID", "IS_ACTIVE", "LAST_STATUS", "RULE_NAME", "TABLE_FQN", "COLUMN_NAME", "RULE_TYPE",
             "RULE_PARAMS", "ROW_FILTER", "THRESHOLD_PCT", "SEVERITY", "DIMENSION", "OWNER"]
    edited = st.data_editor(
        view[shown], hide_index=True, use_container_width=True, height=440, key="rules_editor",
        disabled=[c for c in shown if c not in editable],
        column_config={
            "RULE_ID": st.column_config.NumberColumn("ID", width="small"),
            "IS_ACTIVE": st.column_config.CheckboxColumn("Active"),
            "LAST_STATUS": st.column_config.TextColumn("Last"),
            "THRESHOLD_PCT": st.column_config.NumberColumn("Threshold %", min_value=0.0, max_value=100.0, step=0.1),
            "SEVERITY": st.column_config.SelectboxColumn("Severity", options=ui.SEVERITY_ORDER, required=True),
            "RULE_PARAMS": st.column_config.TextColumn("Params"),
            "RULE_NAME": "Rule", "TABLE_FQN": "Table", "COLUMN_NAME": "Column", "RULE_TYPE": "Type",
            "ROW_FILTER": "Row filter", "DIMENSION": "Dimension", "OWNER": "Owner",
        },
    )

    orig = view[shown].set_index("RULE_ID")[editable]
    new = edited.set_index("RULE_ID")[editable]
    changed = new[(orig.astype(str) != new.astype(str)).any(axis=1)]

    b = st.columns([1, 1, 1, 3])
    if b[0].button(f"Save ({len(changed)})", icon=":material/save:", type="primary", disabled=changed.empty):
        n = db.execute_many(
            f"UPDATE {fq('DQ_RULES')} SET IS_ACTIVE = %s, THRESHOLD_PCT = %s, SEVERITY = %s, OWNER = %s, "
            "UPDATED_AT = CURRENT_TIMESTAMP() WHERE RULE_ID = %s",
            [(bool(r.IS_ACTIVE), float(r.THRESHOLD_PCT or 0), r.SEVERITY, r.OWNER, int(rid)) for rid, r in changed.iterrows()],
        )
        st.toast(f"Updated {n} rule(s)", icon=":material/check:")
        st.rerun()

    pick = b[3].selectbox("Rule for actions", view["RULE_ID"].tolist(), index=None, placeholder="Choose a rule to run or delete",
                          format_func=lambda i: f"#{i} — {rules.set_index('RULE_ID').loc[i, 'RULE_NAME']}", label_visibility="collapsed")
    if b[1].button("Run rule", icon=":material/play_arrow:", disabled=pick is None):
        with st.spinner("Running..."):
            r = db.run_checks(rule_id=int(pick))
        st.toast(f"{r['PASS']} pass · {r['FAIL']} fail · {r['ERROR']} error")

    @st.dialog("Delete rule")
    def confirm_delete(rule_id: int):
        st.write(f"Delete rule **#{rule_id} — {rules.set_index('RULE_ID').loc[rule_id, 'RULE_NAME']}**?")
        st.caption("Historical results are kept in DQ_RESULTS. Tip: deactivating a rule is usually better.")
        if st.button("Delete", type="primary"):
            db.execute(f"DELETE FROM {fq('DQ_RULES')} WHERE RULE_ID = %s", (rule_id,))
            st.rerun()

    if b[2].button("Delete", icon=":material/delete:", disabled=pick is None):
        confirm_delete(int(pick))

# ---- New rule ------------------------------------------------------------------
with tab_new:
    left, right = st.columns([1, 1])
    with left, st.container(border=True):
        ui.section("1. Target")
        dbs = db.query("SHOW TERSE DATABASES")
        db_names = sorted(dbs["NAME"].tolist()) if "NAME" in dbs else []
        database = st.selectbox("Database", db_names, index=None, placeholder="Select database")
        schema = table = None
        columns = pd.DataFrame(columns=["COLUMN_NAME", "DATA_TYPE"])
        if database:
            info = f"{q_ident(database)}.INFORMATION_SCHEMA"
            schemas = db.query(f"SELECT SCHEMA_NAME FROM {info}.SCHEMATA WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' ORDER BY 1")
            schema = st.selectbox("Schema", schemas["SCHEMA_NAME"].tolist(), index=None, placeholder="Select schema")
            if schema:
                tbls = db.query(f"SELECT TABLE_NAME FROM {info}.TABLES WHERE TABLE_SCHEMA = %s ORDER BY 1", (schema,))
                table = st.selectbox("Table / view", tbls["TABLE_NAME"].tolist(), index=None, placeholder="Select table")
                if table:
                    columns = db.query(
                        f"SELECT COLUMN_NAME, DATA_TYPE FROM {info}.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION", (schema, table))

        rule_type = st.selectbox("Rule type", list(RULE_TYPES), format_func=lambda t: f"{t} — {RULE_TYPES[t][1]}")
        col_opts = columns["COLUMN_NAME"].tolist()
        if rule_type == "ROW_COUNT":
            cols = []
        elif rule_type == "UNIQUE":
            cols = st.multiselect("Column(s)", col_opts, help="Select several for a composite key")
        else:
            one = st.selectbox("Column", col_opts, index=None,
                               format_func=lambda c: f"{c}  ·  {columns.set_index('COLUMN_NAME').loc[c, 'DATA_TYPE']}")
            cols = [one] if one else []

    with right, st.container(border=True):
        ui.section("2. Rule logic")
        params: dict = {}
        if rule_type == "ACCEPTED_VALUES":
            vals = st.text_area("Accepted values (one per line or comma-separated)", placeholder="ACTIVE\nINACTIVE")
            params["values"] = [v.strip() for v in vals.replace("\n", ",").split(",") if v.strip()]
        elif rule_type in ("RANGE", "ROW_COUNT"):
            rc = st.columns(2)
            use_min, use_max = rc[0].checkbox("Minimum", True), rc[1].checkbox("Maximum", False)
            if use_min:
                params["min"] = rc[0].number_input("Min", value=0.0 if rule_type == "RANGE" else 1.0, label_visibility="collapsed")
            if use_max:
                params["max"] = rc[1].number_input("Max", value=100.0 if rule_type == "RANGE" else 1e7, label_visibility="collapsed")
        elif rule_type == "REGEX":
            preset = st.selectbox("Preset", list(REGEX_PRESETS))
            params["pattern"] = st.text_input("Pattern", value=REGEX_PRESETS[preset], key=f"rx_{preset}")
        elif rule_type == "FRESHNESS":
            params["max_age_hours"] = st.number_input("Max age (hours)", min_value=0.25, value=24.0, step=1.0)
        elif rule_type == "REFERENTIAL":
            params["ref_table"] = st.text_input("Parent table (DB.SCHEMA.TABLE)")
            params["ref_column"] = st.text_input("Parent column")
        elif rule_type == "CUSTOM_SQL":
            params["failure_condition"] = st.text_area("Failure condition (SQL predicate that is TRUE for bad rows)",
                                                       placeholder="SHIP_DATE < ORDER_DATE")
        else:
            st.caption("No parameters needed for this rule type.")

        row_filter = st.text_input("Row filter (optional WHERE predicate)", placeholder="STATUS <> 'CANCELLED'")
        threshold = st.number_input("Allowed failure % before FAIL", 0.0, 100.0, 0.0, 0.1,
                                    disabled=rule_type in ("FRESHNESS", "ROW_COUNT"))

    with st.container(border=True):
        ui.section("3. Metadata")
        m = st.columns([2, 1, 1, 1.4])
        auto_name = f"{table or 'table'} {', '.join(cols) or ''} {rule_type.replace('_', ' ').lower()}".strip()
        name = m[0].text_input("Rule name", value=auto_name.title() if table else "")
        severity = m[1].selectbox("Severity", ui.SEVERITY_ORDER, index=2)
        dim_default = RULE_TYPES[rule_type][0]
        dimension = m[2].selectbox("Dimension", ui.DIMENSIONS, index=ui.DIMENSIONS.index(dim_default))
        owner = m[3].text_input("Owner", placeholder="Team or person")
        desc = st.text_input("Description", placeholder="Why this rule matters")
        run_now = st.checkbox("Run the rule immediately after saving", value=True)

        needs_col = rule_type not in ("ROW_COUNT",)
        problems = []
        if not (database and schema and table):
            problems.append("select a target table")
        if needs_col and not cols:
            problems.append("select a column")
        if not name:
            problems.append("name the rule")
        if st.button("Create rule", icon=":material/add:", type="primary", disabled=bool(problems)):
            db.execute(INSERT_SQL, (name, desc or None, database, schema, table, ",".join(cols) or None, rule_type,
                                    json.dumps(params), row_filter or None, float(threshold), severity, dimension, owner or None))
            st.success(f"Rule **{name}** created.")
            if run_now:
                new_id = db.execute(f"SELECT MAX(RULE_ID) FROM {fq('DQ_RULES')} WHERE RULE_NAME = %s AND CREATED_BY = CURRENT_USER()",
                                    (name,)).iloc[0, 0]
                with st.spinner("Running..."):
                    r = db.run_checks(rule_id=int(new_id))
                (st.success if r["PASS"] else st.warning)(f"First run: {r['PASS']} pass · {r['FAIL']} fail · {r['ERROR']} error")
        elif problems:
            st.caption("To create: " + ", ".join(problems) + ".")

# ---- Import / export -----------------------------------------------------------
with tab_io:
    left, right = st.columns(2)
    with left, st.container(border=True):
        ui.section("Export")
        st.caption("Download the catalog to version-control rules alongside your code or to copy them to another account.")
        export = rules[RULE_COLS].copy()
        st.download_button("Download rules CSV", export.to_csv(index=False), "dq_rules.csv", "text/csv",
                           icon=":material/download:", use_container_width=True)
    with right, st.container(border=True):
        ui.section("Import")
        st.caption("CSV with columns: " + ", ".join(RULE_COLS) + ". RULE_PARAMS is JSON.")
        up = st.file_uploader("Rules CSV", type="csv", label_visibility="collapsed")
        if up is not None:
            df = pd.read_csv(up)
            missing = [c for c in ["RULE_NAME", "DATABASE_NAME", "SCHEMA_NAME", "TABLE_NAME", "RULE_TYPE"] if c not in df.columns]
            bad_types = sorted(set(df.get("RULE_TYPE", pd.Series(dtype=str)).dropna().str.upper()) - set(RULE_TYPES))
            if missing or bad_types:
                st.error(f"Missing columns: {missing}" if missing else f"Unknown rule types: {bad_types}")
            else:
                for c in RULE_COLS:
                    if c not in df.columns:
                        df[c] = None
                df = df[RULE_COLS].astype(object).where(df[RULE_COLS].notna(), None)
                df["RULE_PARAMS"] = df["RULE_PARAMS"].fillna("{}")
                df["THRESHOLD_PCT"] = df["THRESHOLD_PCT"].fillna(0).astype(float)
                df["SEVERITY"] = df["SEVERITY"].fillna("MEDIUM").str.upper()
                df["DIMENSION"] = df["DIMENSION"].fillna("Validity")
                df["RULE_TYPE"] = df["RULE_TYPE"].str.upper()
                st.dataframe(df, hide_index=True, use_container_width=True, height=200)
                if st.button(f"Import {len(df)} rules", type="primary", icon=":material/upload:"):
                    n = db.execute_many(INSERT_SQL, [tuple(r) for r in df.itertuples(index=False)])
                    st.success(f"Imported {n} rules.")
