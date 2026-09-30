/* =============================================================================
   Data Quality Monitor — 05_alerting.sql   (optional)
   Alert policies, notification history, settings, the NOTIFY_DQ_FAILURES proc,
   and a DQ_NOTIFY task chained AFTER DQ_DAILY_RUN so alerts always follow a run.
   Run AFTER 01-04: tasks in one graph must share an owner, and DQ_DAILY_RUN is
   owned by DQ_ADMIN after 04_roles.sql. If you skipped 04, replace
   "USE ROLE DQ_ADMIN" and the DQ_ADMIN grants below with the role that owns
   DQ_DAILY_RUN. Notification integrations are created once by ACCOUNTADMIN
   (templates at the bottom).
   ============================================================================= */

USE ROLE SYSADMIN;
USE SCHEMA DQ_FRAMEWORK.CORE;
USE WAREHOUSE DQ_WH;

-- -----------------------------------------------------------------------------
-- Policies: who gets notified, about what, and how often.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ_ALERT_CONFIG (
    ALERT_ID        NUMBER AUTOINCREMENT PRIMARY KEY,
    ALERT_NAME      VARCHAR NOT NULL,
    CHANNEL_TYPE    VARCHAR NOT NULL,                  -- EMAIL | WEBHOOK
    RECIPIENTS      VARCHAR NOT NULL,                  -- EMAIL: comma-separated addresses; WEBHOOK: notification integration name
    SEVERITY_FILTER VARCHAR DEFAULT 'CRITICAL,HIGH',
    MIN_SCORE       FLOAT DEFAULT NULL,                -- also alert when a table's DQ score drops below this
    IS_ENABLED      BOOLEAN DEFAULT TRUE,
    CREATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'DQ Monitor alert policies - defines who gets notified and when';

ALTER TABLE DQ_ALERT_CONFIG ADD COLUMN IF NOT EXISTS NOTIFY_MODE VARCHAR DEFAULT 'ALL_FAILURES'
    COMMENT 'ALL_FAILURES | NEW_FAILURES_ONLY (only rules that went PASS -> FAIL since the previous run)';
ALTER TABLE DQ_ALERT_CONFIG ADD COLUMN IF NOT EXISTS SCOPE_TABLES VARCHAR
    COMMENT 'Optional comma-separated table FQNs or patterns (DB.SCHEMA.*); empty = all tables';

CREATE TABLE IF NOT EXISTS DQ_ALERT_HISTORY (
    HISTORY_ID      NUMBER AUTOINCREMENT PRIMARY KEY,
    ALERT_ID        NUMBER,
    ALERT_NAME      VARCHAR,
    CHANNEL_TYPE    VARCHAR,
    RECIPIENTS      VARCHAR,
    FAILURES_COUNT  NUMBER,
    TABLES_AFFECTED VARCHAR,
    MESSAGE_PREVIEW VARCHAR,
    STATUS          VARCHAR,                           -- SENT | FAILED | SKIPPED
    ERROR_MESSAGE   VARCHAR,
    SENT_AT         TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'DQ Monitor alert history - log of every notification evaluation';

CREATE TABLE IF NOT EXISTS DQ_SETTINGS (
    SETTING_KEY     VARCHAR PRIMARY KEY,
    SETTING_VALUE   VARCHAR,
    UPDATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'DQ Monitor key/value settings (e.g. APP_URL used in notification links)';

MERGE INTO DQ_SETTINGS t USING (SELECT 'APP_URL' AS K) s ON t.SETTING_KEY = s.K
WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE) VALUES (s.K, '');
MERGE INTO DQ_SETTINGS t USING (SELECT 'ALERT_HISTORY_RETENTION_DAYS' AS K) s ON t.SETTING_KEY = s.K
WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE) VALUES (s.K, '90');

-- Replaces the earlier cron alert + zero-argument proc.
DROP ALERT IF EXISTS DQ_FAILURE_ALERT;
DROP PROCEDURE IF EXISTS NOTIFY_DQ_FAILURES();

-- -----------------------------------------------------------------------------
-- NOTIFY_DQ_FAILURES
--   CALL NOTIFY_DQ_FAILURES();            -- evaluate all enabled policies (used by DQ_NOTIFY)
--   CALL NOTIFY_DQ_FAILURES(3, TRUE);     -- test one policy: latest results, no time window, [TEST] subject
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE NOTIFY_DQ_FAILURES(
    P_ALERT_ID NUMBER  DEFAULT NULL,
    P_TEST     BOOLEAN DEFAULT FALSE
)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
COMMENT = 'DQ Monitor - evaluates alert policies against the latest results and dispatches email/webhook notifications'
EXECUTE AS CALLER
AS
$$
import fnmatch
import html

CONFIG = "DQ_FRAMEWORK.CORE.DQ_ALERT_CONFIG"
HISTORY = "DQ_FRAMEWORK.CORE.DQ_ALERT_HISTORY"
SETTINGS = "DQ_FRAMEWORK.CORE.DQ_SETTINGS"
LATEST = "DQ_FRAMEWORK.CORE.V_LATEST_RESULTS"
HEALTH = "DQ_FRAMEWORK.CORE.V_TABLE_HEALTH"
RESULTS = "DQ_FRAMEWORK.CORE.DQ_RESULTS"
RECENT_HOURS = 6
SEV_COLOR = {"CRITICAL": "#B91C1C", "HIGH": "#EF4444", "MEDIUM": "#F59E0B", "LOW": "#64748B"}


def _patterns(scope):
    return [p.strip().upper() for p in (scope or "").split(",") if p.strip()]


def _in_scope(fqn, pats):
    return not pats or any(fnmatch.fnmatch(str(fqn).upper(), p) for p in pats)


def _pct(v):
    return f"{float(v):.2f}%" if v is not None else "-"


def _log(session, cfg, count, tables, preview, status, error):
    session.sql(
        f"INSERT INTO {HISTORY} (ALERT_ID, ALERT_NAME, CHANNEL_TYPE, RECIPIENTS, FAILURES_COUNT, "
        "TABLES_AFFECTED, MESSAGE_PREVIEW, STATUS, ERROR_MESSAGE) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?",
        params=[cfg["ALERT_ID"], cfg["ALERT_NAME"], cfg["CHANNEL_TYPE"], cfg["RECIPIENTS"], count,
                tables, (preview or "")[:1000], status, error],
    ).collect()


def _email_html(matched, low, min_score, app_url, alert_name, test):
    rows = "".join(
        f"<tr><td style='padding:8px;border:1px solid #ddd'>"
        f"<span style='color:white;background:{SEV_COLOR.get(f['SEVERITY'], '#64748B')};padding:2px 8px;"
        f"border-radius:10px;font-size:12px'>{html.escape(f['SEVERITY'])}</span></td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{html.escape(str(f['RULE_NAME']))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd'>{html.escape(str(f['TABLE_FQN']))}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;text-align:right'>{_pct(f['FAILED_PCT'])}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;text-align:right'>{_pct(f['THRESHOLD_PCT'])}</td></tr>"
        for f in matched
    )
    tables = len({f["TABLE_FQN"] for f in matched})
    body = ("<div style='font-family:Arial,sans-serif;max-width:860px'>"
            + ("<p style='background:#FFF4E5;padding:8px;border-radius:6px'>This is a <b>test</b> notification.</p>" if test else "")
            + "<h2 style='color:#11567F;margin-bottom:4px'>Data Quality Alert</h2>"
            + f"<p>{len(matched)} check(s) failed across {tables} table(s).</p>")
    if matched:
        body += ("<table style='border-collapse:collapse;width:100%'><tr style='background:#11567F;color:white'>"
                 "<th style='padding:8px;text-align:left'>Severity</th><th style='padding:8px;text-align:left'>Rule</th>"
                 "<th style='padding:8px;text-align:left'>Table</th><th style='padding:8px;text-align:right'>Failed</th>"
                 f"<th style='padding:8px;text-align:right'>Threshold</th></tr>{rows}</table>")
    if low:
        body += f"<h3 style='color:#11567F'>Tables below score threshold ({float(min_score):.0f})</h3><ul>"
        body += "".join(f"<li><b>{html.escape(t)}</b> — score {s:.1f}</li>" for t, s in sorted(low, key=lambda x: x[1]))
        body += "</ul>"
    if app_url:
        body += (f"<p style='margin-top:20px'><a href='{html.escape(app_url)}' style='display:inline-block;padding:10px 24px;"
                 "background:#29B5E8;color:white;text-decoration:none;border-radius:6px;font-weight:bold'>"
                 "Open Data Quality Monitor</a></p>")
    body += f"<p style='color:#999;font-size:12px'>Policy: {html.escape(alert_name)}</p></div>"
    return body


def _text(matched, low, min_score, app_url, test):
    lines = [("[TEST] " if test else "") + f"Data Quality Alert: {len(matched)} check(s) failed"]
    lines += [f"- [{f['SEVERITY']}] {f['RULE_NAME']} on {f['TABLE_FQN']}: {_pct(f['FAILED_PCT'])} failed "
              f"(threshold {_pct(f['THRESHOLD_PCT'])})" for f in matched[:25]]
    if len(matched) > 25:
        lines.append(f"... and {len(matched) - 25} more")
    if low:
        lines.append(f"Tables below score {float(min_score):.0f}: " + ", ".join(f"{t} ({s:.1f})" for t, s in low))
    if app_url:
        lines.append(f"Open the app: {app_url}")
    return "\n".join(lines)


def run(session, p_alert_id, p_test):
    test = bool(p_test)
    settings = {r["SETTING_KEY"]: r["SETTING_VALUE"] for r in session.sql(f"SELECT * FROM {SETTINGS}").collect()}
    app_url = (settings.get("APP_URL") or "").strip()

    if p_alert_id is not None:
        configs = [r.as_dict() for r in session.sql(f"SELECT * FROM {CONFIG} WHERE ALERT_ID = ?", params=[int(p_alert_id)]).collect()]
    else:
        configs = [r.as_dict() for r in session.sql(f"SELECT * FROM {CONFIG} WHERE IS_ENABLED ORDER BY ALERT_ID").collect()]
    if not configs:
        return {"evaluated": 0, "message": "No matching alert policies"}

    window = "" if test else f"AND RUN_TS > DATEADD('hour', -{RECENT_HOURS}, CURRENT_TIMESTAMP())"
    failures = [r.as_dict() for r in session.sql(
        f"SELECT RULE_ID, RULE_NAME, TABLE_FQN, SEVERITY, FAILED_PCT, THRESHOLD_PCT FROM {LATEST} "
        f"WHERE STATUS <> 'PASS' {window} "
        "ORDER BY DECODE(SEVERITY, 'CRITICAL', 1, 'HIGH', 2, 'MEDIUM', 3, 4), TABLE_FQN, RULE_NAME"
    ).collect()]
    previous = {r["RULE_ID"]: r["STATUS"] for r in session.sql(
        f"SELECT RULE_ID, STATUS FROM {RESULTS} "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY RUN_TS DESC) = 2"
    ).collect()}
    health = {r["TABLE_FQN"]: r["DQ_SCORE"] for r in session.sql(f"SELECT TABLE_FQN, DQ_SCORE FROM {HEALTH}").collect()}

    summary = {"SENT": 0, "FAILED": 0, "SKIPPED": 0}
    for cfg in configs:
        channel = (cfg["CHANNEL_TYPE"] or "").upper()
        sev = {s.strip().upper() for s in (cfg["SEVERITY_FILTER"] or "CRITICAL,HIGH").split(",") if s.strip()}
        pats = _patterns(cfg.get("SCOPE_TABLES"))
        mode = (cfg.get("NOTIFY_MODE") or "ALL_FAILURES").upper()
        min_score = cfg.get("MIN_SCORE")

        matched = [f for f in failures if f["SEVERITY"] in sev and _in_scope(f["TABLE_FQN"], pats)]
        if mode == "NEW_FAILURES_ONLY" and not test:
            matched = [f for f in matched if previous.get(f["RULE_ID"]) in (None, "PASS")]
        low = []
        if min_score is not None:
            low = [(t, float(s)) for t, s in health.items()
                   if s is not None and float(s) < float(min_score) and _in_scope(t, pats)]

        if not matched and not low:
            reason = "No new failures since previous run" if mode == "NEW_FAILURES_ONLY" else "No matching failures"
            _log(session, cfg, 0, None, reason, "SKIPPED", None)
            summary["SKIPPED"] += 1
            continue

        tables = ", ".join(sorted({f["TABLE_FQN"] for f in matched}))
        text = _text(matched, low, min_score, app_url, test)
        status, error = "SENT", None
        try:
            if channel == "EMAIL":
                subject = ("[TEST] " if test else "") + f"DQ Alert: {len(matched)} failure(s) detected"
                session.sql("CALL SYSTEM$SEND_EMAIL('DQ_EMAIL_INTEGRATION', ?, ?, ?, 'text/html')",
                            params=[cfg["RECIPIENTS"], subject,
                                    _email_html(matched, low, min_score, app_url, cfg["ALERT_NAME"], test)]).collect()
            elif channel == "WEBHOOK":
                session.sql(
                    "CALL SYSTEM$SEND_SNOWFLAKE_NOTIFICATION("
                    "SNOWFLAKE.NOTIFICATION.TEXT_PLAIN(SNOWFLAKE.NOTIFICATION.SANITIZE_WEBHOOK_CONTENT(?)), "
                    "SNOWFLAKE.NOTIFICATION.INTEGRATION(?))",
                    params=[text, cfg["RECIPIENTS"].strip()]).collect()
            else:
                status, error = "FAILED", f"Unknown channel type: {channel}"
        except Exception as e:
            status, error = "FAILED", str(e)[:2000]
        _log(session, cfg, len(matched), tables, text, status, error)
        summary[status] += 1

    return {"evaluated": len(configs), "test": test, **summary}
$$;

-- -----------------------------------------------------------------------------
-- Grants + notify task chained after the daily check run.
-- -----------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE DQ_ALERT_CONFIG  TO ROLE DQ_ADMIN;
GRANT SELECT, INSERT, DELETE         ON TABLE DQ_ALERT_HISTORY TO ROLE DQ_ADMIN;
GRANT SELECT, INSERT, UPDATE         ON TABLE DQ_SETTINGS      TO ROLE DQ_ADMIN;
GRANT SELECT ON TABLE DQ_ALERT_CONFIG  TO ROLE DQ_VIEWER;
GRANT SELECT ON TABLE DQ_ALERT_HISTORY TO ROLE DQ_VIEWER;
GRANT SELECT ON TABLE DQ_SETTINGS      TO ROLE DQ_VIEWER;
GRANT USAGE ON PROCEDURE NOTIFY_DQ_FAILURES(NUMBER, BOOLEAN) TO ROLE DQ_ADMIN;
GRANT CREATE TASK ON SCHEMA DQ_FRAMEWORK.CORE TO ROLE DQ_ADMIN;

-- Tasks in one graph must share an owner; DQ_ADMIN owns DQ_DAILY_RUN (04_roles.sql).
USE ROLE DQ_ADMIN;
ALTER TASK DQ_FRAMEWORK.CORE.DQ_DAILY_RUN SUSPEND;   -- a root must be suspended to add children
CREATE OR REPLACE TASK DQ_FRAMEWORK.CORE.DQ_NOTIFY
  WAREHOUSE = DQ_WH
  COMMENT = 'Data Quality Monitor - evaluates alert policies right after DQ_DAILY_RUN'
  AFTER DQ_FRAMEWORK.CORE.DQ_DAILY_RUN
AS
  CALL DQ_FRAMEWORK.CORE.NOTIFY_DQ_FAILURES(NULL, FALSE);
ALTER TASK DQ_FRAMEWORK.CORE.DQ_NOTIFY RESUME;       -- child ready; the schedule stays off until the root is resumed

-- Alert history retention: daily delete of rows older than ALERT_HISTORY_RETENTION_DAYS (default 90).
CREATE OR REPLACE TASK DQ_FRAMEWORK.CORE.DQ_ALERT_HISTORY_RETENTION
  WAREHOUSE = DQ_WH
  SCHEDULE = 'USING CRON 30 5 * * * UTC'
  COMMENT = 'Data Quality Monitor - purges DQ_ALERT_HISTORY rows older than the retention setting'
AS
  DELETE FROM DQ_FRAMEWORK.CORE.DQ_ALERT_HISTORY
  WHERE SENT_AT < DATEADD('day', -COALESCE(
      (SELECT TRY_TO_NUMBER(SETTING_VALUE) FROM DQ_FRAMEWORK.CORE.DQ_SETTINGS
       WHERE SETTING_KEY = 'ALERT_HISTORY_RETENTION_DAYS'), 90), CURRENT_TIMESTAMP());
ALTER TASK DQ_FRAMEWORK.CORE.DQ_ALERT_HISTORY_RETENTION RESUME;

-- Turn on the daily schedule (runs checks, then notifications):
--   SELECT SYSTEM$TASK_DEPENDENTS_ENABLE('DQ_FRAMEWORK.CORE.DQ_DAILY_RUN');
-- Run the whole chain once now:
--   EXECUTE TASK DQ_FRAMEWORK.CORE.DQ_DAILY_RUN;

-- -----------------------------------------------------------------------------
-- ACCOUNTADMIN, once: notification integrations + usage for the task owner.
-- -----------------------------------------------------------------------------
-- Email (recipients must be verified users in this account):
-- USE ROLE ACCOUNTADMIN;
-- CREATE NOTIFICATION INTEGRATION IF NOT EXISTS DQ_EMAIL_INTEGRATION
--   TYPE = EMAIL ENABLED = TRUE ALLOWED_RECIPIENTS = ('you@company.com')
--   COMMENT = 'DQ Monitor email notifications';
-- GRANT USAGE ON INTEGRATION DQ_EMAIL_INTEGRATION TO ROLE DQ_ADMIN;
--
-- Webhook (Slack example; store the URL path as a secret):
-- CREATE SECRET DQ_FRAMEWORK.CORE.DQ_SLACK_SECRET TYPE = GENERIC_STRING SECRET_STRING = 'T000/B000/XXXX';
-- CREATE NOTIFICATION INTEGRATION DQ_SLACK_WEBHOOK
--   TYPE = WEBHOOK ENABLED = TRUE
--   WEBHOOK_URL = 'https://hooks.slack.com/services/SNOWFLAKE_WEBHOOK_SECRET'
--   WEBHOOK_SECRET = DQ_FRAMEWORK.CORE.DQ_SLACK_SECRET
--   WEBHOOK_BODY_TEMPLATE = '{"text": "SNOWFLAKE_WEBHOOK_MESSAGE"}'
--   WEBHOOK_HEADERS = ('Content-Type' = 'application/json')
--   COMMENT = 'DQ Monitor Slack notifications';
-- GRANT USAGE ON INTEGRATION DQ_SLACK_WEBHOOK TO ROLE DQ_ADMIN;
-- Then create a WEBHOOK policy in the app with recipients = DQ_SLACK_WEBHOOK.
