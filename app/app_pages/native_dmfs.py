"""Native DMFs: results from Snowflake Data Metric Functions, if any are attached."""

import altair as alt
import streamlit as st

from lib import db, ui

ui.hero("Native Data Metric Functions",
        "Results from Snowflake's built-in DMFs (NULL_COUNT, DUPLICATE_COUNT, FRESHNESS, ROW_COUNT, custom DMFs).")

with st.expander("About DMFs and how they relate to this app"):
    st.markdown(
        """
Snowflake **Data Metric Functions** run serverlessly on a schedule you attach to a table and write to
`SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS`. This app's rule engine and DMFs are complementary —
use whichever fits; this page shows DMF results side by side.

```sql
ALTER TABLE MY_DB.MY_SCHEMA.ORDERS SET DATA_METRIC_SCHEDULE = '60 MINUTE';
ALTER TABLE MY_DB.MY_SCHEMA.ORDERS ADD DATA METRIC FUNCTION SNOWFLAKE.CORE.NULL_COUNT ON (CUSTOMER_ID);
```
Viewing results requires the `SNOWFLAKE.DATA_QUALITY_MONITORING_VIEWER` (or `..._ADMIN`) application role.
"""
    )

days = st.segmented_control("Window", [1, 7, 30], default=7, format_func=lambda d: f"{d}D", key="dmf_days") or 7
try:
    res = db.query(
        """SELECT MEASUREMENT_TIME, TABLE_DATABASE || '.' || TABLE_SCHEMA || '.' || TABLE_NAME AS TABLE_FQN,
                  METRIC_DATABASE || '.' || METRIC_SCHEMA || '.' || METRIC_NAME AS METRIC,
                  ARRAY_TO_STRING(ARGUMENT_NAMES, ', ') AS ARGUMENTS, VALUE
           FROM SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS
           WHERE MEASUREMENT_TIME >= DATEADD('day', -?, CURRENT_TIMESTAMP())
           ORDER BY MEASUREMENT_TIME DESC LIMIT 20000""",
        (days,),
    )
except Exception as e:
    st.warning("DMF results are not accessible with the current role.")
    st.caption(str(e))
    st.stop()

if res.empty:
    st.info("No DMF measurements in this window. Attach DMFs to a table (see above) to populate this page.")
    st.stop()

res["VALUE"] = res["VALUE"].astype(float)
latest = res.sort_values("MEASUREMENT_TIME").groupby(["TABLE_FQN", "METRIC", "ARGUMENTS"], dropna=False).tail(1)

c = st.columns(3)
with c[0]:
    ui.kpi("Tables with DMFs", latest["TABLE_FQN"].nunique(), "", ui.NAVY)
with c[1]:
    ui.kpi("Metrics tracked", len(latest), "table × metric × column", ui.PRIMARY)
with c[2]:
    ui.kpi("Measurements", f"{len(res):,}", f"last {days} days", ui.PRIMARY)
st.write("")

with st.container(border=True):
    ui.section("Latest value per metric")
    st.dataframe(latest.sort_values(["TABLE_FQN", "METRIC"]), hide_index=True, use_container_width=True,
                 column_config={"MEASUREMENT_TIME": st.column_config.DatetimeColumn("Measured", format="MMM D, HH:mm")})

with st.container(border=True):
    ui.section("Metric trend")
    keys = (latest["TABLE_FQN"] + " · " + latest["METRIC"].str.split(".").str[-1] + "(" + latest["ARGUMENTS"].fillna("") + ")").tolist()
    pick = st.selectbox("Metric", keys)
    row = latest.iloc[keys.index(pick)]
    s = res[(res["TABLE_FQN"] == row["TABLE_FQN"]) & (res["METRIC"] == row["METRIC"]) &
            (res["ARGUMENTS"].fillna("") == (row["ARGUMENTS"] or ""))]
    st.altair_chart(
        alt.Chart(s).mark_line(point=True, color=ui.PRIMARY, strokeWidth=2.5)
        .encode(x=alt.X("MEASUREMENT_TIME:T", title=None), y=alt.Y("VALUE:Q", title="Value"),
                tooltip=["MEASUREMENT_TIME:T", "VALUE"])
        .properties(height=260),
        use_container_width=True,
    )
