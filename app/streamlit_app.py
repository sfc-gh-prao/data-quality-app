"""Data Quality Monitor — Streamlit in Snowflake app entry point."""

import streamlit as st

from lib import db, ui

st.set_page_config(page_title=db.APP_TITLE, page_icon=":material/verified:", layout="wide")
ui.inject_css()

pages = {
    "Monitor": [
        st.Page("app_pages/overview.py", title="Overview", icon=":material/dashboard:", default=True),
        st.Page("app_pages/table_health.py", title="Table Health", icon=":material/table_chart:"),
        st.Page("app_pages/run_history.py", title="Run History", icon=":material/history:"),
    ],
    "Configure": [
        st.Page("app_pages/rules.py", title="Rules", icon=":material/rule:"),
        st.Page("app_pages/native_dmfs.py", title="Native DMFs", icon=":material/monitor_heart:"),
    ],
}
nav = st.navigation(pages)

with st.sidebar:
    st.markdown(f"### :material/verified: {db.APP_TITLE}")
    st.caption(f"Framework: `{db.FRAMEWORK}`")
    if st.button("Refresh data", icon=":material/refresh:", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if st.button("Run all checks now", icon=":material/play_arrow:", type="primary", use_container_width=True):
        with st.spinner("Running all active checks..."):
            try:
                r = db.run_checks()
                st.success(f"{r['rules_executed']} checks — {r['PASS']} pass, {r['FAIL']} fail, {r['ERROR']} error")
            except Exception as e:
                st.error(f"Run failed: {e}")

try:
    db.get_conn()
except Exception as e:
    st.error(f"Could not connect to Snowflake: {e}")
    st.stop()

nav.run()
