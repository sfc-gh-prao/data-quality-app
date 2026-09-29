"""Overview: organization-wide data quality at a glance."""

import altair as alt
import pandas as pd
import streamlit as st

from lib import db, ui
from lib.db import fq

db.require_framework()
ui.hero(db.APP_TITLE, "Weighted data quality score, trends, and open issues across every monitored table.")

days = st.segmented_control("Window", [7, 14, 30, 90], default=30, format_func=lambda d: f"{d}D", key="ov_days") or 30

latest = db.query(f"SELECT * FROM {fq('V_LATEST_RESULTS')}")
health = db.query(f"SELECT * FROM {fq('V_TABLE_HEALTH')} ORDER BY DQ_SCORE, CRITICAL_ISSUES DESC")
trend = db.query(
    f"""SELECT RUN_DATE,
               ROUND(100 * SUM(IFF(STATUS = 'PASS', WEIGHT, 0)) / NULLIF(SUM(WEIGHT), 0), 1) AS DQ_SCORE,
               COUNT(*) AS CHECKS, COUNT_IF(STATUS <> 'PASS') AS ISSUES
        FROM {fq('V_DAILY_RESULTS')}
        WHERE RUN_DATE >= DATEADD('day', -?, CURRENT_DATE())
        GROUP BY RUN_DATE ORDER BY RUN_DATE""",
    (days,),
)

if latest.empty:
    st.info("No check results yet. Add rules on the **Rules** page, then click **Run all checks now**.")
    st.stop()

# ---- KPIs --------------------------------------------------------------------
latest["PASSW"] = latest["WEIGHT"].where(latest["STATUS"] == "PASS", 0)
score = round(100 * latest["PASSW"].sum() / latest["WEIGHT"].sum(), 1)
n_fail = int((latest["STATUS"] == "FAIL").sum())
n_err = int((latest["STATUS"] == "ERROR").sum())
n_crit = int(((latest["STATUS"] != "PASS") & (latest["SEVERITY"] == "CRITICAL")).sum())
prev = trend["DQ_SCORE"].iloc[-2] if len(trend) > 1 else None
delta = f"{score - float(prev):+.1f} pts vs prior day" if prev is not None else "severity-weighted"

c = st.columns(5)
with c[0]:
    ui.kpi("DQ Score", f"{score:.1f}", delta, ui.score_color(score))
with c[1]:
    ui.kpi("Checks passing", f"{len(latest) - n_fail - n_err}/{len(latest)}", f"{100 * (len(latest) - n_fail - n_err) / len(latest):.0f}% pass rate", ui.GOOD)
with c[2]:
    ui.kpi("Failing checks", n_fail, f"{n_err} errored", ui.BAD if n_fail else ui.GOOD)
with c[3]:
    ui.kpi("Critical issues", n_crit, "CRITICAL severity not passing", "#B91C1C" if n_crit else ui.GOOD)
with c[4]:
    ui.kpi("Tables monitored", len(health), f"Last run {ui.fmt_ts(latest['RUN_TS'].max())}", ui.NAVY)

st.write("")

# ---- Trend + status mix ------------------------------------------------------
left, right = st.columns([2.2, 1])
with left, st.container(border=True):
    ui.section(f"DQ score trend — last {days} days")
    if trend.empty:
        st.caption("No history in this window.")
    else:
        trend["DQ_SCORE"] = trend["DQ_SCORE"].astype(float)
        st.altair_chart(ui.score_trend_chart(trend), use_container_width=True)
        st.caption("Dashed line = 95 target. Score weights checks by severity: CRITICAL 8, HIGH 4, MEDIUM 2, LOW 1.")

with right, st.container(border=True):
    ui.section("Latest status mix")
    mix = latest.groupby("STATUS").size().reset_index(name="N")
    donut = (
        alt.Chart(mix).mark_arc(innerRadius=62, cornerRadius=4, padAngle=0.02)
        .encode(theta="N:Q", color=alt.Color("STATUS:N", scale=ui.status_scale(), legend=alt.Legend(orient="bottom", title=None)),
                tooltip=["STATUS", "N"])
        .properties(height=250)
    )
    label = alt.Chart(pd.DataFrame({"t": [f"{score:.0f}"]})).mark_text(fontSize=34, fontWeight="bold", color=ui.NAVY).encode(text="t:N")
    st.altair_chart(donut + label, use_container_width=True)

# ---- Dimensions + tables -----------------------------------------------------
left, right = st.columns([1, 1.4])
with left, st.container(border=True):
    ui.section("Score by quality dimension")
    dim = latest.groupby("DIMENSION").agg(PASSW=("PASSW", "sum"), W=("WEIGHT", "sum"), CHECKS=("RULE_ID", "count")).reset_index()
    dim["DQ_SCORE"] = (100 * dim["PASSW"] / dim["W"]).round(1)
    bars = (
        alt.Chart(dim).mark_bar(cornerRadiusEnd=6, height=22)
        .encode(
            y=alt.Y("DIMENSION:N", sort="-x", title=None),
            x=alt.X("DQ_SCORE:Q", scale=alt.Scale(domain=[0, 100]), title="Score"),
            color=alt.Color("DQ_SCORE:Q", scale=ui.score_scale(), legend=None),
            tooltip=["DIMENSION", "DQ_SCORE", "CHECKS"],
        )
    )
    text = bars.mark_text(align="left", dx=4, color=ui.NAVY, fontWeight="bold").encode(text=alt.Text("DQ_SCORE:Q", format=".0f"))
    st.altair_chart((bars + text).properties(height=260), use_container_width=True)

with right, st.container(border=True):
    ui.section("Table leaderboard")
    st.dataframe(
        health[["TABLE_FQN", "DQ_SCORE", "CHECKS", "FAILED", "ERRORS", "CRITICAL_ISSUES", "LAST_RUN_TS"]],
        hide_index=True, use_container_width=True, height=260,
        column_config={
            "TABLE_FQN": st.column_config.TextColumn("Table", width="large"),
            "DQ_SCORE": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
            "CHECKS": "Checks", "FAILED": "Failed", "ERRORS": "Errors", "CRITICAL_ISSUES": "Critical",
            "LAST_RUN_TS": st.column_config.DatetimeColumn("Last run", format="MMM D, HH:mm"),
        },
    )

# ---- Heatmap -----------------------------------------------------------------
heat = db.query(
    f"SELECT RUN_DATE, TABLE_FQN, DQ_SCORE, CHECKS, FAILED FROM {fq('V_DAILY_TABLE_SCORE')} "
    "WHERE RUN_DATE >= DATEADD('day', -?, CURRENT_DATE())",
    (days,),
)
if not heat.empty:
    with st.container(border=True):
        ui.section("Daily score heatmap by table")
        heat["DQ_SCORE"] = heat["DQ_SCORE"].astype(float)
        hm = (
            alt.Chart(heat).mark_rect(cornerRadius=3, stroke="white", strokeWidth=1.5)
            .encode(
                x=alt.X("RUN_DATE:O", timeUnit="yearmonthdate", title=None, axis=alt.Axis(format="%b %d", labelAngle=-45)),
                y=alt.Y("TABLE_FQN:N", title=None),
                color=alt.Color("DQ_SCORE:Q", scale=ui.score_scale(), legend=alt.Legend(title="Score")),
                tooltip=[alt.Tooltip("RUN_DATE:T", title="Date"), "TABLE_FQN", "DQ_SCORE", "CHECKS", "FAILED"],
            )
            .properties(height=max(140, 42 * heat["TABLE_FQN"].nunique()))
        )
        st.altair_chart(hm, use_container_width=True)

# ---- Open issues -------------------------------------------------------------
issues = latest[latest["STATUS"] != "PASS"].copy()
issues["SEV_RANK"] = issues["SEVERITY"].map({s: i for i, s in enumerate(ui.SEVERITY_ORDER)})
issues = issues.sort_values(["SEV_RANK", "FAILED_PCT"], ascending=[True, False])
with st.container(border=True):
    ui.section(f"Open issues ({len(issues)})")
    if issues.empty:
        st.success("All checks are passing.", icon=":material/check_circle:")
    cols = st.columns(2)
    for i, (_, r) in enumerate(issues.head(12).iterrows()):
        if r["STATUS"] == "ERROR":
            meta = f"{r['TABLE_FQN']} · {r['RULE_TYPE']} · error: {str(r['ERROR_MESSAGE'])[:120]}"
        else:
            meta = (f"{r['TABLE_FQN']}.{r['COLUMN_NAME'] or '*'} · {r['RULE_TYPE']} · "
                    f"{ui.fmt_int(r['FAILED_ROWS'])} rows ({float(r['FAILED_PCT']):.3g}%) vs threshold {float(r['THRESHOLD_PCT']):g}%")
        with cols[i % 2]:
            ui.issue_card(r["RULE_NAME"], meta, r["SEVERITY"], r["STATUS"])
    if len(issues) > 12:
        st.caption(f"+ {len(issues) - 12} more — see **Table Health** or **Run History**.")
