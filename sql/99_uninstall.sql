/* =============================================================================
   Data Quality Monitor — 99_uninstall.sql
   Removes everything this project created. Irreversible (beyond Time Travel).
   ============================================================================= */

USE ROLE SYSADMIN;
ALTER TASK IF EXISTS DQ_FRAMEWORK.CORE.DQ_DAILY_RUN SUSPEND;
DROP STREAMLIT IF EXISTS DQ_FRAMEWORK.CORE.DATA_QUALITY_MONITOR;
DROP DATABASE IF EXISTS DQ_SAMPLE;       -- sample data only
DROP DATABASE IF EXISTS DQ_FRAMEWORK;    -- rules, results, alert policies/history/settings, procedures, tasks, app
DROP WAREHOUSE IF EXISTS DQ_WH;

USE ROLE ACCOUNTADMIN;
DROP INTEGRATION IF EXISTS DQ_EMAIL_INTEGRATION;   -- plus any webhook integrations you created (e.g. DQ_SLACK_WEBHOOK)

USE ROLE SECURITYADMIN;
DROP ROLE IF EXISTS DQ_VIEWER;
DROP ROLE IF EXISTS DQ_ADMIN;
