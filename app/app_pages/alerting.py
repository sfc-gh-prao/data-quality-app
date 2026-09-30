"""Alerting: configure notifications for data quality failures."""

import re

import pandas as pd
import streamlit as st

from lib import db, ui
from lib.db import fq

db.require_framework()
ui.hero("Alerting", "Get notified when data quality drops. Policies run right after each scheduled check run — by email or webhook.")

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
MODES = {"ALL_FAILURES": "Every failing check, every run", "NEW_FAILURES_ONLY": "Only checks that newly failed (PASS → FAIL)"}
ROOT_TASK, NOTIFY_TASK = "DQ_DAILY_RUN", "DQ_NOTIFY"
RO = not db.is_admin()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


_errors: list[str] = []


def safe(sql: str, params: tuple | None = None) -> pd.DataFrame:
    """Uncached read; missing objects/privileges return empty, but the reason is shown on the page."""
    try:
        return db.run_uncached(sql, params)
    except Exception as e:
        _errors.append(f"{type(e).__name__}: {str(e)[:200]}")
        return pd.DataFrame()


# ── Live infrastructure state (uncached) ─────────────────────────────────────

integrations = safe("SHOW NOTIFICATION INTEGRATIONS")
email_ok = not integrations.empty and "DQ_EMAIL_INTEGRATION" in integrations["NAME"].str.upper().tolist()
webhook_ints = (integrations[integrations["TYPE"].str.upper() == "WEBHOOK"]["NAME"].tolist()
                if not integrations.empty and "TYPE" in integrations.columns else [])

allowed = []
if email_ok:
    d = safe("DESCRIBE INTEGRATION DQ_EMAIL_INTEGRATION")
    if not d.empty:
        row = d[d["PROPERTY"].str.upper() == "ALLOWED_RECIPIENTS"]
        if not row.empty:
            allowed = [e.strip().strip("[]\"' ").lower() for e in str(row.iloc[0]["PROPERTY_VALUE"]).split(",") if e.strip("[] ")]

tasks = safe("SHOW TASKS IN SCHEMA DQ_FRAMEWORK.CORE")
task_state = {r["NAME"]: r for _, r in tasks.iterrows()} if not tasks.empty else {}
root, notify = task_state.get(ROOT_TASK), task_state.get(NOTIFY_TASK)
schedule_on = root is not None and str(root["STATE"]).lower() == "started"


def bad_emails(recipients: str) -> tuple[list, list]:
    """Return (malformed, not_in_allowed_list)."""
    emails = [e.strip() for e in recipients.split(",") if e.strip()]
    malformed = [e for e in emails if not EMAIL_RE.match(e)]
    blocked = [e for e in emails if EMAIL_RE.match(e) and allowed and e.lower() not in allowed]
    return malformed, blocked


# ── Status panel ─────────────────────────────────────────────────────────────

if _errors:
    with st.expander(f"{len(_errors)} status check(s) failed — details", icon=":material/error:"):
        for e in dict.fromkeys(_errors):
            st.code(e, language=None)
        st.caption("If this says `module 'lib.db' has no attribute ...`, the app is still running an older version: "
                   "reboot it from the app's ⋮ menu (or close and reopen it).")

ui.section("Status")
db.read_only_notice("manage alert policies and the schedule")
sc = st.columns(3)

with sc[0], st.container(border=True):
    st.markdown("**Email channel**")
    if email_ok:
        st.success("DQ_EMAIL_INTEGRATION active", icon=":material/check_circle:")
        st.caption("Allowed recipients: " + (", ".join(allowed) if allowed else "any verified user"))
    else:
        st.warning("Email integration not created", icon=":material/warning:")
    with st.popover("Setup / add recipients SQL", use_container_width=True):
        st.caption("Run once as ACCOUNTADMIN. Recipients must be verified users in this account.")
        st.code("USE ROLE ACCOUNTADMIN;\n"
                "CREATE OR REPLACE NOTIFICATION INTEGRATION DQ_EMAIL_INTEGRATION\n"
                "  TYPE = EMAIL ENABLED = TRUE\n"
                f"  ALLOWED_RECIPIENTS = ({', '.join(repr(a) for a in allowed) or chr(39) + 'you@company.com' + chr(39)})\n"
                "  COMMENT = 'DQ Monitor email notifications';\n"
                "GRANT USAGE ON INTEGRATION DQ_EMAIL_INTEGRATION TO ROLE DQ_ADMIN;", language="sql")

with sc[1], st.container(border=True):
    st.markdown("**Webhook channel**")
    if webhook_ints:
        st.success(f"{len(webhook_ints)} webhook integration(s)", icon=":material/check_circle:")
        st.caption(", ".join(webhook_ints))
    else:
        st.info("No webhook integrations yet", icon=":material/webhook:")
    with st.popover("Slack / Teams webhook SQL", use_container_width=True):
        st.caption("Run once as ACCOUNTADMIN, then create a WEBHOOK policy using the integration name.")
        st.code("USE ROLE ACCOUNTADMIN;\n"
                "CREATE SECRET DQ_FRAMEWORK.CORE.DQ_SLACK_SECRET\n"
                "  TYPE = GENERIC_STRING SECRET_STRING = 'T000/B000/XXXX';  -- part after /services/\n"
                "CREATE NOTIFICATION INTEGRATION DQ_SLACK_WEBHOOK\n"
                "  TYPE = WEBHOOK ENABLED = TRUE\n"
                "  WEBHOOK_URL = 'https://hooks.slack.com/services/SNOWFLAKE_WEBHOOK_SECRET'\n"
                "  WEBHOOK_SECRET = DQ_FRAMEWORK.CORE.DQ_SLACK_SECRET\n"
                "  WEBHOOK_BODY_TEMPLATE = '{\"text\": \"SNOWFLAKE_WEBHOOK_MESSAGE\"}'\n"
                "  WEBHOOK_HEADERS = ('Content-Type' = 'application/json');\n"
                "GRANT USAGE ON INTEGRATION DQ_SLACK_WEBHOOK TO ROLE DQ_ADMIN;", language="sql")

with sc[2], st.container(border=True):
    st.markdown("**Schedule**")
    if root is None or notify is None:
        st.error("Tasks missing — run sql/05_alerting.sql", icon=":material/error:")
    else:
        st.markdown(f"`{ROOT_TASK}` → `{NOTIFY_TASK}`  \n{root['SCHEDULE']}")
        (st.success if schedule_on else st.info)("Daily schedule ON" if schedule_on else "Daily schedule OFF",
                                                  icon=":material/schedule:")
        b = st.columns(2)
        if schedule_on:
            if b[0].button("Turn off", key="sched_off", use_container_width=True, disabled=RO):
                db.execute(f"ALTER TASK DQ_FRAMEWORK.CORE.{ROOT_TASK} SUSPEND")
                st.rerun()
        elif b[0].button("Turn on", key="sched_on", type="primary", use_container_width=True, disabled=RO):
            try:
                db.execute(f"SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DQ_FRAMEWORK.CORE.{ROOT_TASK}')")
                st.rerun()
            except Exception as e:
                st.error(f"Could not enable: {e}")
        if b[1].button("Run now", key="sched_run", use_container_width=True, help="Run checks, then notifications", disabled=RO):
            try:
                db.execute(f"EXECUTE TASK DQ_FRAMEWORK.CORE.{ROOT_TASK}")
                st.toast("Chain started — results and alerts land in a minute or two.", icon=":material/play_arrow:")
            except Exception as e:
                st.error(f"Could not start: {e}")

runs = safe("SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME, ERROR_MESSAGE "
            "FROM TABLE(DQ_FRAMEWORK.INFORMATION_SCHEMA.TASK_HISTORY(RESULT_LIMIT => 1000, "
            "SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP()))) "
            f"WHERE DATABASE_NAME = 'DQ_FRAMEWORK' AND NAME IN ('{ROOT_TASK}', '{NOTIFY_TASK}') "
            "ORDER BY SCHEDULED_TIME DESC LIMIT 20")
last_eval = safe(f"SELECT STATUS, COUNT(*) AS N, MAX(SENT_AT) AS AT FROM {fq('DQ_ALERT_HISTORY')} "
                 f"WHERE SENT_AT >= (SELECT DATEADD('minute', -5, MAX(SENT_AT)) FROM {fq('DQ_ALERT_HISTORY')}) GROUP BY STATUS")
if not last_eval.empty:
    parts = " · ".join(f"{int(r.N)} {r.STATUS.lower()}" for r in last_eval.itertuples())
    st.caption(f"Last evaluation {ui.fmt_ts(last_eval['AT'].max())}: {parts}")
if not runs.empty:
    with st.expander("Recent task runs"):
        st.dataframe(runs, use_container_width=True, hide_index=True,
                     column_config={"SCHEDULED_TIME": st.column_config.DatetimeColumn("Scheduled", format="MMM DD, HH:mm"),
                                    "COMPLETED_TIME": st.column_config.DatetimeColumn("Completed", format="MMM DD, HH:mm")})

with st.expander("Settings"):
    settings = safe(f"SELECT SETTING_KEY, SETTING_VALUE FROM {fq('DQ_SETTINGS')}")
    cur_all = settings.set_index("SETTING_KEY")["SETTING_VALUE"].to_dict() if not settings.empty else {}
    cur = cur_all.get("APP_URL") or ""
    cur_days = int(float(cur_all.get("ALERT_HISTORY_RETENTION_DAYS") or 90))
    sc2 = st.columns([3, 1])
    url = sc2[0].text_input("App URL used for the 'Open Data Quality Monitor' link in notifications", value=cur, disabled=RO)
    days = sc2[1].number_input("Keep alert history (days)", 7, 3650, cur_days, 1, disabled=RO,
                               help="DQ_ALERT_HISTORY_RETENTION deletes older rows daily")
    if st.button("Save settings", key="save_settings", disabled=RO or (url == cur and days == cur_days)):
        for k, v in (("APP_URL", url), ("ALERT_HISTORY_RETENTION_DAYS", str(int(days)))):
            db.execute(f"MERGE INTO {fq('DQ_SETTINGS')} t USING (SELECT ? AS K, ? AS V) s ON t.SETTING_KEY = s.K "
                       "WHEN MATCHED THEN UPDATE SET SETTING_VALUE = s.V, UPDATED_AT = CURRENT_TIMESTAMP() "
                       "WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE) VALUES (s.K, s.V)", (k, v))
        st.toast("Saved", icon=":material/check:")

st.divider()


# ── Policy form (shared by create + edit) ────────────────────────────────────

known_tables = safe(f"SELECT DISTINCT DATABASE_NAME || '.' || SCHEMA_NAME || '.' || TABLE_NAME AS T, "
                    f"DATABASE_NAME || '.' || SCHEMA_NAME || '.*' AS S FROM {fq('DQ_RULES')} ORDER BY 1")
scope_options = sorted(set(known_tables["S"]) | set(known_tables["T"])) if not known_tables.empty else []


def policy_form(prefix: str, d: dict) -> dict | None:
    """Render the policy fields; return a values dict when valid, else None (with reasons shown)."""
    c = st.columns([1, 2])
    channel = c[0].selectbox("Channel", ["EMAIL", "WEBHOOK"], index=["EMAIL", "WEBHOOK"].index(d.get("CHANNEL_TYPE", "EMAIL")),
                             format_func=lambda x: {"EMAIL": "Email", "WEBHOOK": "Webhook (Slack, Teams, ...)"}[x], key=f"{prefix}_ch")
    problems = []
    if channel == "EMAIL":
        recipients = c[1].text_input("Recipients (comma-separated)", value=d.get("RECIPIENTS", "") if d.get("CHANNEL_TYPE") == "EMAIL" else "",
                                     placeholder=", ".join(allowed) or "user@company.com", key=f"{prefix}_rcpt")
        malformed, blocked = bad_emails(recipients)
        if malformed:
            problems.append("fix malformed addresses: " + ", ".join(malformed))
        if blocked:
            st.warning("Not in the integration's allowed list (will fail to send): " + ", ".join(blocked)
                       + ". Add them via **Setup / add recipients SQL** above.", icon=":material/warning:")
        if not email_ok:
            st.warning("Email integration isn't set up yet — see Status above.")
    else:
        opts = webhook_ints or ["(none — create one first)"]
        cur = d.get("RECIPIENTS") if d.get("RECIPIENTS") in webhook_ints else opts[0]
        recipients = c[1].selectbox("Webhook integration", opts, index=opts.index(cur), key=f"{prefix}_wh")
        if not webhook_ints:
            problems.append("create a webhook integration")

    c2 = st.columns([1.3, 1.7])
    sev = c2[0].multiselect("Severities", SEVERITIES, key=f"{prefix}_sev",
                            default=[s for s in str(d.get("SEVERITY_FILTER", "CRITICAL,HIGH")).split(",") if s in SEVERITIES])
    scope_cur = [s.strip() for s in str(d.get("SCOPE_TABLES") or "").split(",") if s.strip()]
    scope = c2[1].multiselect("Scope (empty = all tables)", sorted(set(scope_options) | set(scope_cur)), default=scope_cur,
                              placeholder="All tables", key=f"{prefix}_scope", help="Pick tables or whole schemas (DB.SCHEMA.*)")

    c3 = st.columns([1.7, 1.3])
    mode = c3[0].radio("Notify on", list(MODES), format_func=MODES.get, key=f"{prefix}_mode",
                       index=list(MODES).index(d.get("NOTIFY_MODE") or "ALL_FAILURES"))
    use_score = c3[1].checkbox("Also alert on low table score", value=pd.notna(d.get("MIN_SCORE")) and d.get("MIN_SCORE") is not None,
                               key=f"{prefix}_use_score")
    min_score = c3[1].number_input("Min DQ score", 0, 100, int(d["MIN_SCORE"]) if use_score and pd.notna(d.get("MIN_SCORE")) else 80,
                                   5, key=f"{prefix}_score", disabled=not use_score)

    default_name = f"{channel.title()} — {', '.join(sev) or 'no severities'}" + (f" — {len(scope)} scope(s)" if scope else "")
    name = st.text_input("Policy name", value=d.get("ALERT_NAME") or default_name, key=f"{prefix}_name")

    if not recipients or recipients.startswith("(none"):
        problems.append("set recipients")
    if not sev and not use_score:
        problems.append("pick a severity or a score threshold")
    if not name:
        problems.append("name the policy")
    if problems:
        st.caption("To save: " + "; ".join(problems) + ".")
        return None
    return {"ALERT_NAME": name, "CHANNEL_TYPE": channel, "RECIPIENTS": recipients, "SEVERITY_FILTER": ",".join(sev),
            "SCOPE_TABLES": ",".join(scope) or None, "NOTIFY_MODE": mode,
            "MIN_SCORE": float(min_score) if use_score else None}


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_pol, tab_new, tab_hist = st.tabs([":material/list: Policies", ":material/add_circle: New policy", ":material/history: History"])

with tab_pol:
    policies = safe(f"SELECT * FROM {fq('DQ_ALERT_CONFIG')} ORDER BY ALERT_ID")
    if policies.empty:
        st.info("No alert policies yet. Create one in **New policy**.")
    else:
        k = st.columns(3)
        ui_enabled = int(policies["IS_ENABLED"].sum())
        with k[0]:
            ui.kpi("Policies", len(policies), f"{ui_enabled} enabled", ui.NAVY)
        with k[1]:
            ui.kpi("Channels", policies["CHANNEL_TYPE"].nunique(), ", ".join(sorted(policies["CHANNEL_TYPE"].unique())), ui.PRIMARY)
        with k[2]:
            ui.kpi("Schedule", "ON" if schedule_on else "OFF", "alerts fire after each daily run" if schedule_on else "use Run now or Test",
                   ui.GOOD if schedule_on else ui.WARN)
        st.write("")

        view = policies[["ALERT_ID", "IS_ENABLED", "ALERT_NAME", "CHANNEL_TYPE", "RECIPIENTS", "SEVERITY_FILTER",
                         "SCOPE_TABLES", "NOTIFY_MODE", "MIN_SCORE"]]
        edited = st.data_editor(
            view, use_container_width=True, hide_index=True, key="pol_editor", height=min(320, 40 + 35 * len(view)),
            disabled=list(view.columns) if RO else [c for c in view.columns if c != "IS_ENABLED"],
            column_config={"ALERT_ID": st.column_config.NumberColumn("ID", width="small"),
                           "IS_ENABLED": st.column_config.CheckboxColumn("Enabled"), "ALERT_NAME": "Policy",
                           "CHANNEL_TYPE": "Channel", "RECIPIENTS": "Recipients", "SEVERITY_FILTER": "Severities",
                           "SCOPE_TABLES": "Scope", "NOTIFY_MODE": "Mode",
                           "MIN_SCORE": st.column_config.NumberColumn("Min score", format="%d")},
        )
        toggled = edited[edited["IS_ENABLED"] != view["IS_ENABLED"]]
        if not toggled.empty:
            db.execute_many(f"UPDATE {fq('DQ_ALERT_CONFIG')} SET IS_ENABLED = ?, UPDATED_AT = CURRENT_TIMESTAMP() WHERE ALERT_ID = ?",
                            [(bool(r.IS_ENABLED), int(r.ALERT_ID)) for r in toggled.itertuples()])
            st.rerun()

        by_id = policies.set_index("ALERT_ID")
        a = st.columns([2.5, 1, 1, 1])
        pick = a[0].selectbox("Policy", policies["ALERT_ID"].tolist(), index=None, placeholder="Choose a policy to test, edit, or delete",
                              format_func=lambda i: f"#{i} — {by_id.loc[i, 'ALERT_NAME']}", label_visibility="collapsed")

        if a[1].button("Send test", icon=":material/send:", disabled=pick is None or RO, use_container_width=True):
            with st.spinner("Sending test for this policy only..."):
                try:
                    db.execute(f"CALL {fq('NOTIFY_DQ_FAILURES')}(?, TRUE)", (int(pick),))
                    last = safe(f"SELECT STATUS, FAILURES_COUNT, ERROR_MESSAGE FROM {fq('DQ_ALERT_HISTORY')} "
                                "WHERE ALERT_ID = ? ORDER BY SENT_AT DESC LIMIT 1", (int(pick),))
                    if not last.empty and last.iloc[0]["STATUS"] == "SENT":
                        st.success(f"Test sent ({int(last.iloc[0]['FAILURES_COUNT'])} failures included). Subject is prefixed [TEST].")
                    elif not last.empty and last.iloc[0]["STATUS"] == "SKIPPED":
                        st.info("Nothing matched this policy's severities/scope in the latest results, so no message was sent.")
                    else:
                        st.error(f"Send failed: {last.iloc[0]['ERROR_MESSAGE'] if not last.empty else 'unknown'}")
                except Exception as e:
                    st.error(f"Test failed: {e}")

        @st.dialog("Edit policy", width="large")
        def edit_dialog(pid: int):
            vals = policy_form(f"edit_{pid}", by_id.loc[pid].to_dict() | {"ALERT_ID": pid})
            if st.button("Save changes", type="primary", disabled=vals is None):
                db.execute(f"UPDATE {fq('DQ_ALERT_CONFIG')} SET ALERT_NAME = ?, CHANNEL_TYPE = ?, RECIPIENTS = ?, "
                           "SEVERITY_FILTER = ?, SCOPE_TABLES = ?, NOTIFY_MODE = ?, MIN_SCORE = ?, UPDATED_AT = CURRENT_TIMESTAMP() "
                           "WHERE ALERT_ID = ?",
                           (vals["ALERT_NAME"], vals["CHANNEL_TYPE"], vals["RECIPIENTS"], vals["SEVERITY_FILTER"],
                            vals["SCOPE_TABLES"], vals["NOTIFY_MODE"], vals["MIN_SCORE"], pid))
                st.rerun()

        @st.dialog("Delete policy")
        def delete_dialog(pid: int):
            st.write(f"Delete **#{pid} — {by_id.loc[pid, 'ALERT_NAME']}**? History rows are kept.")
            if st.button("Delete", type="primary"):
                db.execute(f"DELETE FROM {fq('DQ_ALERT_CONFIG')} WHERE ALERT_ID = ?", (pid,))
                st.rerun()

        if a[2].button("Edit", icon=":material/edit:", disabled=pick is None or RO, use_container_width=True):
            edit_dialog(int(pick))
        if a[3].button("Delete", icon=":material/delete:", disabled=pick is None or RO, use_container_width=True):
            delete_dialog(int(pick))

with tab_new:
    if RO:
        st.info("Only admins can create alert policies.", icon=":material/lock:")
    else:
        with st.container(border=True):
            vals = policy_form("new", {})
            if st.button("Create policy", icon=":material/add:", type="primary", disabled=vals is None):
                db.execute(f"INSERT INTO {fq('DQ_ALERT_CONFIG')} (ALERT_NAME, CHANNEL_TYPE, RECIPIENTS, SEVERITY_FILTER, "
                           "SCOPE_TABLES, NOTIFY_MODE, MIN_SCORE) SELECT ?, ?, ?, ?, ?, ?, ?",
                           (vals["ALERT_NAME"], vals["CHANNEL_TYPE"], vals["RECIPIENTS"], vals["SEVERITY_FILTER"],
                            vals["SCOPE_TABLES"], vals["NOTIFY_MODE"], vals["MIN_SCORE"]))
                st.success(f"Policy **{vals['ALERT_NAME']}** created. Use **Send test** on the Policies tab to try it.")

with tab_hist:
    hist = safe(f"SELECT HISTORY_ID, SENT_AT, ALERT_ID, ALERT_NAME, CHANNEL_TYPE, STATUS, FAILURES_COUNT, "
                f"TABLES_AFFECTED, MESSAGE_PREVIEW, ERROR_MESSAGE FROM {fq('DQ_ALERT_HISTORY')} ORDER BY SENT_AT DESC LIMIT 500")
    if hist.empty:
        st.info("No notifications evaluated yet.")
    else:
        f = st.columns([2, 2, 1])
        pf = f[0].multiselect("Policy", sorted(hist["ALERT_NAME"].dropna().unique()), placeholder="All policies")
        sf = f[1].multiselect("Status", ["SENT", "FAILED", "SKIPPED"], default=["SENT", "FAILED"])
        v = hist
        if pf:
            v = v[v["ALERT_NAME"].isin(pf)]
        if sf:
            v = v[v["STATUS"].isin(sf)]
        k = st.columns(4)
        k[0].metric("Sent", int((hist["STATUS"] == "SENT").sum()))
        k[1].metric("Failed", int((hist["STATUS"] == "FAILED").sum()))
        k[2].metric("Skipped", int((hist["STATUS"] == "SKIPPED").sum()))
        k[3].metric("Latest", ui.fmt_ts(hist["SENT_AT"].iloc[0]))
        st.dataframe(v.drop(columns=["MESSAGE_PREVIEW"]), use_container_width=True, hide_index=True,
                     height=min(380, 40 + 35 * len(v)),
                     column_config={"SENT_AT": st.column_config.DatetimeColumn("When", format="MMM DD, HH:mm"),
                                    "FAILURES_COUNT": st.column_config.NumberColumn("Failures", format="%d")})
        ui.section("Recent messages")
        for r in v.head(10).itertuples():
            with st.expander(f"{ui.fmt_ts(r.SENT_AT)} · {r.STATUS} · {r.ALERT_NAME}"):
                if r.ERROR_MESSAGE:
                    st.error(r.ERROR_MESSAGE)
                st.code(r.MESSAGE_PREVIEW or "(no message)", language=None)
