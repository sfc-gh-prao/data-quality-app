/* =============================================================================
   Data Quality Monitor — 01_setup.sql
   Creates the framework database, rule catalog, results history, and views.

   CUSTOMIZE: The framework lives in DQ_FRAMEWORK.CORE by default. To use a
   different location, find/replace "DQ_FRAMEWORK" (and optionally "CORE")
   across the sql/ folder and set DQ_FRAMEWORK_SCHEMA in app/.streamlit/secrets
   or the app's environment (see README).
   ============================================================================= */

USE ROLE SYSADMIN;   -- or any role with CREATE DATABASE

CREATE WAREHOUSE IF NOT EXISTS DQ_WH
  WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE
  COMMENT = 'Data Quality Monitor - check execution + app queries';

CREATE DATABASE IF NOT EXISTS DQ_FRAMEWORK COMMENT = 'Data Quality Monitor framework';
CREATE SCHEMA IF NOT EXISTS DQ_FRAMEWORK.CORE;
USE SCHEMA DQ_FRAMEWORK.CORE;
USE WAREHOUSE DQ_WH;

-- -----------------------------------------------------------------------------
-- Rule catalog: one row per check. Rules are pure configuration — add rules for
-- any table in any database the executing role can read.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ_RULES (
    RULE_ID        NUMBER AUTOINCREMENT START 1 INCREMENT 1 PRIMARY KEY,
    RULE_NAME      VARCHAR NOT NULL,
    DESCRIPTION    VARCHAR,
    DATABASE_NAME  VARCHAR NOT NULL,
    SCHEMA_NAME    VARCHAR NOT NULL,
    TABLE_NAME     VARCHAR NOT NULL,
    COLUMN_NAME    VARCHAR,              -- comma-separated for composite UNIQUE
    RULE_TYPE      VARCHAR NOT NULL,     -- see RULE TYPES below
    RULE_PARAMS    VARIANT,              -- JSON parameters for the rule type
    ROW_FILTER     VARCHAR,              -- optional WHERE predicate to scope the check
    THRESHOLD_PCT  FLOAT DEFAULT 0,      -- max % failing rows allowed before FAIL
    SEVERITY       VARCHAR DEFAULT 'MEDIUM',   -- CRITICAL | HIGH | MEDIUM | LOW
    DIMENSION      VARCHAR DEFAULT 'Validity', -- Completeness | Uniqueness | Validity | Consistency | Timeliness | Volume
    OWNER          VARCHAR,
    IS_ACTIVE      BOOLEAN DEFAULT TRUE,
    CREATED_BY     VARCHAR DEFAULT CURRENT_USER(),
    CREATED_AT     TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    UPDATED_AT     TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = $$RULE TYPES and RULE_PARAMS:
  NOT_NULL        {}
  UNIQUE          {}                                     (COLUMN_NAME may be "A,B")
  ACCEPTED_VALUES {"values": ["A","B"]}
  RANGE           {"min": 0, "max": 100}                 (either bound optional)
  REGEX           {"pattern": "^[^@]+@[^@]+$"}
  FRESHNESS       {"max_age_hours": 24}                  (COLUMN_NAME = timestamp column)
  ROW_COUNT       {"min": 1000, "max": 10000000}         (no column)
  REFERENTIAL     {"ref_table": "DB.SCHEMA.TABLE", "ref_column": "ID"}
  CUSTOM_SQL      {"failure_condition": "SHIP_DATE < ORDER_DATE"}$$;

-- -----------------------------------------------------------------------------
-- Results history: one row per rule per run. Denormalized so history survives
-- rule edits/deletes and the app can query it without joins.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ_RESULTS (
    RESULT_ID      NUMBER AUTOINCREMENT START 1 INCREMENT 1,
    RUN_ID         VARCHAR NOT NULL,
    RUN_TS         TIMESTAMP_LTZ NOT NULL,
    RULE_ID        NUMBER NOT NULL,
    RULE_NAME      VARCHAR,
    TABLE_FQN      VARCHAR,
    COLUMN_NAME    VARCHAR,
    RULE_TYPE      VARCHAR,
    DIMENSION      VARCHAR,
    SEVERITY       VARCHAR,
    TOTAL_ROWS     NUMBER,
    FAILED_ROWS    NUMBER,
    FAILED_PCT     FLOAT,
    THRESHOLD_PCT  FLOAT,
    STATUS         VARCHAR,              -- PASS | FAIL | ERROR
    ERROR_MESSAGE  VARCHAR,
    DURATION_MS    NUMBER,
    CHECK_SQL      VARCHAR,              -- the SQL that computed the metric
    SAMPLE_SQL     VARCHAR,              -- SQL that returns example failing rows
    TRIGGERED_BY   VARCHAR               -- SCHEDULE | MANUAL | BACKFILL
)
CLUSTER BY (TO_DATE(RUN_TS));

-- -----------------------------------------------------------------------------
-- Views used by the app (all scoring logic lives here so it is reusable from
-- dashboards, alerts, or other BI tools).
-- -----------------------------------------------------------------------------

-- Severity weights for the weighted DQ score.
CREATE OR REPLACE VIEW V_SEVERITY_WEIGHTS AS
SELECT * FROM VALUES ('CRITICAL', 8), ('HIGH', 4), ('MEDIUM', 2), ('LOW', 1)
  AS t(SEVERITY, WEIGHT);

-- Most recent result for every active rule.
CREATE OR REPLACE VIEW V_LATEST_RESULTS AS
SELECT r.*, w.WEIGHT, ru.OWNER, ru.DESCRIPTION
FROM DQ_RESULTS r
JOIN DQ_RULES ru ON ru.RULE_ID = r.RULE_ID AND ru.IS_ACTIVE
LEFT JOIN V_SEVERITY_WEIGHTS w ON w.SEVERITY = r.SEVERITY
QUALIFY ROW_NUMBER() OVER (PARTITION BY r.RULE_ID ORDER BY r.RUN_TS DESC) = 1;

-- Last result per rule per day (basis for trends).
CREATE OR REPLACE VIEW V_DAILY_RESULTS AS
SELECT TO_DATE(r.RUN_TS) AS RUN_DATE, r.*, w.WEIGHT
FROM DQ_RESULTS r
LEFT JOIN V_SEVERITY_WEIGHTS w ON w.SEVERITY = r.SEVERITY
QUALIFY ROW_NUMBER() OVER (PARTITION BY r.RULE_ID, TO_DATE(r.RUN_TS) ORDER BY r.RUN_TS DESC) = 1;

-- Weighted DQ score (0-100) per table per day. ERROR counts as not passing.
CREATE OR REPLACE VIEW V_DAILY_TABLE_SCORE AS
SELECT RUN_DATE, TABLE_FQN,
       COUNT(*)                                        AS CHECKS,
       COUNT_IF(STATUS = 'PASS')                       AS PASSED,
       COUNT_IF(STATUS = 'FAIL')                       AS FAILED,
       COUNT_IF(STATUS = 'ERROR')                      AS ERRORS,
       ROUND(100 * SUM(IFF(STATUS = 'PASS', WEIGHT, 0)) / NULLIF(SUM(WEIGHT), 0), 1) AS DQ_SCORE
FROM V_DAILY_RESULTS
GROUP BY RUN_DATE, TABLE_FQN;

-- Current health per table.
CREATE OR REPLACE VIEW V_TABLE_HEALTH AS
SELECT TABLE_FQN,
       COUNT(*)                                        AS CHECKS,
       COUNT_IF(STATUS = 'PASS')                       AS PASSED,
       COUNT_IF(STATUS = 'FAIL')                       AS FAILED,
       COUNT_IF(STATUS = 'ERROR')                      AS ERRORS,
       COUNT_IF(STATUS <> 'PASS' AND SEVERITY = 'CRITICAL') AS CRITICAL_ISSUES,
       ROUND(100 * SUM(IFF(STATUS = 'PASS', WEIGHT, 0)) / NULLIF(SUM(WEIGHT), 0), 1) AS DQ_SCORE,
       MAX(RUN_TS)                                     AS LAST_RUN_TS
FROM V_LATEST_RESULTS
GROUP BY TABLE_FQN;
