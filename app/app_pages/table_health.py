"""Table Health: drill into one table's checks, trends, and failing rows."""

import altair as alt
import streamlit as st

from lib import db, ui
from lib.db import fq

db.require_framework()

health = db.query(f"SELECT * FROM {fq('V_TABLE_HEALTH')} ORDER BY TABLE_FQN")
if health.empty:
    ui.hero("Table Health", "No results yet.")
    st.info("Add rules on the **Rules** page and run them to see table health.")
    st.stop()

tables = health["TABLE_FQN"].tolist()
default = st.query_params.get("table")
table = st.selectbox("Table", tables, index=tables.index(default) if default in tables else 0)
st.query_params["table"] = table
h = health.set_index("TABLE_FQN").loc[table]

ui.hero(table, f"{int(h['CHECKS'])} active checks · last run {ui.fmt_ts(h['LAST_RUN_TS'])}")

c = st.columns([1, 1, 1, 1, 1.2])
with c[0]:
    ui.kpi("DQ Score", f"{float(h['DQ_SCORE']):.1f}", "severity-weighted", ui.score_color(h["DQ_SCORE"]))
with c[1]:
    ui.kpi("Passing", int(h["PASSED"]), f"of {int(h['CHECKS'])} checks", ui.GOOD)
with c[2]:
    ui.kpi("Failing", int(h["FAILED"]), f"{int(h['ERRORS'])} errored", ui.BAD if h["FAILED"] else ui.GOOD)
with c[3]:
    ui.kpi("Critical", int(h["CRITICAL_ISSUES"]), "not passing", "#B91C1C" if h["CRITICAL_ISSUES"] else ui.GOOD)
with c[4]:
    st.write("")
    if st.button("Run checks", help="Run every active check on this table now", icon=":material/play_arrow:", type="primary", use_container_width=True,
                 disabled=not db.is_admin()):
        with st.spinner(f"Running checks on {table}..."):
            try:
                r = db.run_checks(table_fqn=table)
                st.toast(f"{r['PASS']} pass · {r['FAIL']} fail · {r['ERROR']} error", icon=":material/task_alt:")
                st.rerun()
            except Exception as e:
                st.error(f"Run failed: {e}")

st.write("")
checks = db.query(f"SELECT * FROM {fq('V_LATEST_RESULTS')} WHERE TABLE_FQN = ? ORDER BY RULE_ID", (table,))
trend = db.query(
    f"SELECT RUN_DATE, DQ_SCORE FROM {fq('V_DAILY_TABLE_SCORE')} WHERE TABLE_FQN = ? "
    "AND RUN_DATE >= DATEADD('day', -90, CURRENT_DATE()) ORDER BY RUN_DATE",
    (table,),
)

left, right = st.columns([1.6, 1])
with left, st.container(border=True):
    ui.section("Score trend (90 days)")
    if len(trend):
        trend["DQ_SCORE"] = trend["DQ_SCORE"].astype(float)
        st.altair_chart(ui.score_trend_chart(trend, height=230), use_container_width=True)
with right, st.container(border=True):
    ui.section("Checks by dimension")
    dim = checks.groupby(["DIMENSION", "STATUS"]).size().reset_index(name="N")
    st.altair_chart(
        alt.Chart(dim).mark_bar(cornerRadiusEnd=4)
        .encode(y=alt.Y("DIMENSION:N", title=None), x=alt.X("N:Q", title="Checks", axis=alt.Axis(tickMinStep=1)),
                color=alt.Color("STATUS:N", scale=ui.status_scale(), legend=alt.Legend(orient="bottom", title=None)),
                tooltip=["DIMENSION", "STATUS", "N"])
        .properties(height=230),
        use_container_width=True,
    )

with st.container(border=True):
    ui.section("Checks — select a row to drill in")
    view = checks[["STATUS", "SEVERITY", "RULE_NAME", "COLUMN_NAME", "RULE_TYPE", "DIMENSION",
                   "FAILED_ROWS", "TOTAL_ROWS", "FAILED_PCT", "THRESHOLD_PCT", "OWNER"]].copy()
    view["STATUS"] = view["STATUS"].map({"PASS": "✅ PASS", "FAIL": "❌ FAIL", "ERROR": "⚠️ ERROR"})
    sel = st.dataframe(
        view, hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row", key="th_checks",
        column_config={
            "FAILED_PCT": st.column_config.NumberColumn("Failed %", format="%.3f"),
            "THRESHOLD_PCT": st.column_config.NumberColumn("Threshold %", format="%.2f"),
            "FAILED_ROWS": st.column_config.NumberColumn("Failed rows", format="%d"),
            "TOTAL_ROWS": st.column_config.NumberColumn("Rows checked", format="%d"),
            "RULE_NAME": st.column_config.TextColumn("Rule", width="medium"),
        },
    )

rows = sel.selection.rows if sel and sel.selection else []
if rows:
    rule = checks.iloc[rows[0]]
    hist = db.query(
        f"SELECT RUN_DATE, FAILED_PCT, THRESHOLD_PCT, STATUS, FAILED_ROWS FROM {fq('V_DAILY_RESULTS')} "
        "WHERE RULE_ID = ? AND RUN_DATE >= DATEADD('day', -90, CURRENT_DATE()) ORDER BY RUN_DATE",
        (int(rule["RULE_ID"]),),
    )
    with st.container(border=True):
        ui.section(f"{rule['RULE_NAME']}")
        st.markdown(
            f"{ui.pill(rule['SEVERITY'], ui.SEVERITY_COLORS.get(rule['SEVERITY'], ui.MUTED))} "
            f"{ui.pill(rule['STATUS'], ui.STATUS_COLORS.get(rule['STATUS'], ui.MUTED))} &nbsp; "
            f"{rule['DESCRIPTION'] or ''}",
            unsafe_allow_html=True,
        )
        if rule["STATUS"] == "ERROR":
            st.error(rule["ERROR_MESSAGE"])
        t1, t2, t3 = st.tabs(["Failure rate history", "Failing rows sample", "Check SQL"])
        with t1:
            if len(hist):
                base = alt.Chart(hist).encode(x=alt.X("RUN_DATE:T", title=None, axis=alt.Axis(format="%b %d")))
                line = base.mark_line(color=ui.PRIMARY, strokeWidth=2.5).encode(y=alt.Y("FAILED_PCT:Q", title="Failed %"))
                pts = base.mark_circle(size=60).encode(
                    y="FAILED_PCT:Q", color=alt.Color("STATUS:N", scale=ui.status_scale(), legend=None),
                    tooltip=["RUN_DATE:T", "FAILED_PCT", "FAILED_ROWS", "STATUS"],
                )
                thr = base.mark_line(strokeDash=[6, 4], color=ui.BAD).encode(y="THRESHOLD_PCT:Q")
                st.altair_chart((line + thr + pts).properties(height=240), use_container_width=True)
                st.caption("Red dashed line = threshold. Points above it are failures.")
        with t2:
            if not db.is_admin():
                st.info("Failing rows contain real data values, so they're shown to admins only.", icon=":material/lock:")
            elif rule["SAMPLE_SQL"] and st.toggle("Show up to 100 example rows", key=f"sample_{rule['RULE_ID']}"):
                try:
                    # Uncached: raw rows must not sit in the app-wide cache shared by all viewers.
                    st.dataframe(db.run_uncached(rule["SAMPLE_SQL"]), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.error(f"Could not load sample: {e}")
        with t3:
            st.code(rule["CHECK_SQL"] or "-- not available", language="sql")
            st.caption("Sample query")
            st.code(rule["SAMPLE_SQL"] or "-- not available", language="sql")
