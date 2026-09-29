/* =============================================================================
   Data Quality Monitor — 04_roles.sql   (recommended)
   DQ_ADMIN  : manage rules, run checks, own the task
   DQ_VIEWER : read-only dashboards
   Run as SECURITYADMIN (roles) + the owner of DQ_FRAMEWORK (grants).
   ============================================================================= */

USE ROLE SECURITYADMIN;
CREATE ROLE IF NOT EXISTS DQ_ADMIN  COMMENT = 'Data Quality Monitor - administrators';
CREATE ROLE IF NOT EXISTS DQ_VIEWER COMMENT = 'Data Quality Monitor - read-only users';
GRANT ROLE DQ_VIEWER TO ROLE DQ_ADMIN;
GRANT ROLE DQ_ADMIN  TO ROLE SYSADMIN;

USE ROLE SYSADMIN;
GRANT USAGE ON WAREHOUSE DQ_WH TO ROLE DQ_VIEWER;
GRANT USAGE ON DATABASE DQ_FRAMEWORK TO ROLE DQ_VIEWER;
GRANT USAGE ON SCHEMA DQ_FRAMEWORK.CORE TO ROLE DQ_VIEWER;
GRANT SELECT ON ALL TABLES IN SCHEMA DQ_FRAMEWORK.CORE TO ROLE DQ_VIEWER;
GRANT SELECT ON ALL VIEWS  IN SCHEMA DQ_FRAMEWORK.CORE TO ROLE DQ_VIEWER;

-- Admins edit rules, write results, and run the engine
GRANT INSERT, UPDATE, DELETE ON TABLE DQ_FRAMEWORK.CORE.DQ_RULES   TO ROLE DQ_ADMIN;
GRANT INSERT                 ON TABLE DQ_FRAMEWORK.CORE.DQ_RESULTS TO ROLE DQ_ADMIN;
GRANT USAGE ON PROCEDURE DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS(VARCHAR, NUMBER, VARCHAR) TO ROLE DQ_ADMIN;

-- The engine runs with CALLER's rights: grant DQ_ADMIN read access on each
-- monitored database, e.g. for the sample data
-- (REMOVE these 3 lines if you skipped 03_sample_data.sql):
GRANT USAGE ON DATABASE DQ_SAMPLE TO ROLE DQ_ADMIN;
GRANT USAGE ON ALL SCHEMAS IN DATABASE DQ_SAMPLE TO ROLE DQ_ADMIN;
GRANT SELECT ON ALL TABLES IN DATABASE DQ_SAMPLE TO ROLE DQ_ADMIN;

-- Let admins own and operate the schedule
GRANT OWNERSHIP ON TASK DQ_FRAMEWORK.CORE.DQ_DAILY_RUN TO ROLE DQ_ADMIN COPY CURRENT GRANTS;
USE ROLE ACCOUNTADMIN;
GRANT EXECUTE TASK ON ACCOUNT TO ROLE DQ_ADMIN;

-- After deploying the app (see README):
-- GRANT USAGE ON STREAMLIT DQ_FRAMEWORK.CORE.DATA_QUALITY_MONITOR TO ROLE DQ_VIEWER;
-- Viewers can open every page; write actions (save/run) will fail for them by design.
