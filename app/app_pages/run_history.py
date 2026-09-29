"""Run History: every check execution, filterable, with SQL detail."""

import altair as alt
import streamlit as st

from lib import db, ui
from lib.db import fq

db.require_framework()
ui.hero("Run History", "Every check execution — filter, trend, and inspect the SQL that produced each result.")

opts = db.query(f"SELECT DISTINCT TABLE_FQN FROM {fq('DQ_RESULTS')} ORDER BY 1")
f = st.columns([1, 2, 1.4, 1.6, 1.4])
days = f[0].selectbox("Window", [1, 7, 14, 30, 90, 365], index=3, format_func=lambda d: f"Last {d} days")
tables = f[1].multiselect("Tables", opts["TABLE_FQN"].tolist(), placeholder="All tables")
statuses = f[2].multiselect("Status", ["PASS", "FAIL", "ERROR"], placeholder="All")
sevs = f[3].multiselect("Severity", ui.SEVERITY_ORDER, placeholder="All")
trig = f[4].multiselect("Triggered by", ["SCHEDULE", "MANUAL", "BACKFILL"], placeholder="All")

# Build filters with bind parameters only.
where, params = ["RUN_TS >= DATEADD('day', -%s, CURRENT_TIMESTAMP())"], [days]
for col, vals in (("TABLE_FQN", tables), ("STATUS", statuses), ("SEVERITY", sevs), ("TRIGGERED_BY", trig)):
    if vals:
        where.append(f"{col} IN ({', '.join(['%s'] * len(vals))})")
        params.extend(vals)
clause = " AND ".join(where)

res = db.query(
    f"SELECT RUN_TS, RUN_ID, STATUS, SEVERITY, TABLE_FQN, RULE_NAME, COLUMN_NAME, RULE_TYPE, DIMENSION, "
    f"FAILED_ROWS, TOTAL_ROWS, FAILED_PCT, THRESHOLD_PCT, DURATION_MS, TRIGGERED_BY, ERROR_MESSAGE, CHECK_SQL, SAMPLE_SQL "
    f"FROM {fq('DQ_RESULTS')} WHERE {clause} ORDER BY RUN_TS DESC LIMIT 5000",
    tuple(params),
)

if res.empty:
    st.info("No results match these filters.")
    st.stop()

c = st.columns(4)
with c[0]:
    ui.kpi("Executions", f"{len(res):,}", f"{res['RUN_ID'].nunique()} runs", ui.NAVY)
with c[1]:
    pr = 100 * (res["STATUS"] == "PASS").mean()
    ui.kpi("Pass rate", f"{pr:.1f}%", "of executions", ui.score_color(pr))
with c[2]:
    ui.kpi("Failures", f"{(res['STATUS'] == 'FAIL').sum():,}", f"{(res['STATUS'] == 'ERROR').sum()} errors", ui.BAD)
with c[3]:
    ui.kpi("Avg duration", f"{res['DURATION_MS'].mean() / 1000:.2f}s", "per check", ui.PRIMARY)

st.write("")
with st.container(border=True):
    ui.section("Executions per day by status")
    res["RUN_DATE"] = res["RUN_TS"].dt.date.astype(str)
    daily = res.groupby(["RUN_DATE", "STATUS"]).size().reset_index(name="N")
    st.altair_chart(
        alt.Chart(daily).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(x=alt.X("RUN_DATE:T", title=None, axis=alt.Axis(format="%b %d")), y=alt.Y("N:Q", title="Checks"),
                color=alt.Color("STATUS:N", scale=ui.status_scale(), legend=alt.Legend(orient="top", title=None)),
                order=alt.Order("STATUS:N", sort="descending"),
                tooltip=["RUN_DATE:T", "STATUS", "N"])
        .properties(height=220),
        use_container_width=True,
    )

with st.container(border=True):
    ui.section("Results — select a row for details")
    view = res.drop(columns=["CHECK_SQL", "SAMPLE_SQL", "ERROR_MESSAGE", "RUN_ID", "RUN_DATE"])
    sel = st.dataframe(
        view, hide_index=True, use_container_width=True, height=380, on_select="rerun",
        selection_mode="single-row", key="rh_table",
        column_config={
            "RUN_TS": st.column_config.DatetimeColumn("Run time", format="MMM D, HH:mm"),
            "FAILED_PCT": st.column_config.NumberColumn("Failed %", format="%.3f"),
            "THRESHOLD_PCT": st.column_config.NumberColumn("Threshold %", format="%.2f"),
            "DURATION_MS": st.column_config.NumberColumn("ms", format="%d"),
        },
    )
    st.download_button("Download CSV", view.to_csv(index=False), "dq_results.csv", "text/csv", icon=":material/download:")

rows = sel.selection.rows if sel and sel.selection else []
if rows:
    r = res.iloc[rows[0]]
    with st.container(border=True):
        ui.section(f"{r['RULE_NAME']} — {ui.fmt_ts(r['RUN_TS'])}")
        st.caption(f"Run ID `{r['RUN_ID']}` · {r['TRIGGERED_BY']}")
        if r["ERROR_MESSAGE"]:
            st.error(r["ERROR_MESSAGE"])
        st.code(r["CHECK_SQL"] or "-- not available", language="sql")
