"""Data Quality Monitor — Streamlit in Snowflake app entry point."""

import streamlit as st

from lib import db, ui

st.set_page_config(page_title=db.APP_TITLE, page_icon=":material/verified:", layout="wide")
ui.inject_css()
db.init_viewer()  # must run at the top level: the caller's-rights token is only valid at session start

pages = {
    "Monitor": [
        st.Page("app_pages/overview.py", title="Overview", icon=":material/dashboard:", default=True),
        st.Page("app_pages/table_health.py", title="Table Health", icon=":material/table_chart:"),
        st.Page("app_pages/run_history.py", title="Run History", icon=":material/history:"),
    ],
    "Configure": [
        st.Page("app_pages/schema_explorer.py", title="Schema Explorer", icon=":material/explore:"),
        st.Page("app_pages/rules.py", title="Rules", icon=":material/rule:"),
        st.Page("app_pages/native_dmfs.py", title="Native DMFs", icon=":material/monitor_heart:"),
    ],
    "Alerts": [
        st.Page("app_pages/alerting.py", title="Alerting", icon=":material/notifications:"),
    ],
}
nav = st.navigation(pages)

with st.sidebar:
    st.markdown(f"### :material/verified: {db.APP_TITLE}")
    st.caption(f"Framework: `{db.FRAMEWORK}`")
    real_admin = bool(st.session_state.get("dq_is_admin"))
    access = "admin" if db.is_admin() else "read-only"
    st.caption(f"Viewer: {st.session_state.get('dq_viewer')} · {access}")
    st.caption(f"Access comes from holding `{db.ADMIN_ROLE}` (default + secondary roles), "
               "not the role selected in Snowsight.")
    if real_admin:
        st.toggle("Preview as read-only viewer", key="dq_preview_readonly",
                  help="See the app as a non-admin would. Only removes rights, for this session only.")
    if st.session_state.get("dq_viewer_note"):
        st.warning(st.session_state["dq_viewer_note"], icon=":material/lock:")
    if st.button("Refresh data", icon=":material/refresh:", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    if st.button("Run all checks now", icon=":material/play_arrow:", type="primary", use_container_width=True,
                 disabled=not db.is_admin()):
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
