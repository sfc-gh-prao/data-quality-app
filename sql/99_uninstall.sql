/* =============================================================================
   Data Quality Monitor — 99_uninstall.sql
   Removes everything this project created. Irreversible (beyond Time Travel).
   ============================================================================= */

USE ROLE SYSADMIN;
ALTER TASK IF EXISTS DQ_FRAMEWORK.CORE.DQ_DAILY_RUN SUSPEND;
DROP STREAMLIT IF EXISTS DQ_FRAMEWORK.CORE.DATA_QUALITY_MONITOR;
DROP DATABASE IF EXISTS DQ_SAMPLE;       -- sample data only
DROP DATABASE IF EXISTS DQ_FRAMEWORK;    -- rules, results history, procedure, task, app
DROP WAREHOUSE IF EXISTS DQ_WH;

USE ROLE SECURITYADMIN;
DROP ROLE IF EXISTS DQ_VIEWER;
DROP ROLE IF EXISTS DQ_ADMIN;
