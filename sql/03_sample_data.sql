/* =============================================================================
   Data Quality Monitor — 03_sample_data.sql   (OPTIONAL — for demo/testing)
   Builds DQ_SAMPLE.RETAIL with ~1.8M rows of synthetic retail data containing
   deliberately injected defects, registers ~28 rules against it, runs them, and
   backfills 30 days of history so the trends in the app are populated.

   Safe to re-run. Remove with:  DROP DATABASE DQ_SAMPLE;
                                  DELETE FROM DQ_FRAMEWORK.CORE.DQ_RULES WHERE DATABASE_NAME = 'DQ_SAMPLE';
                                  DELETE FROM DQ_FRAMEWORK.CORE.DQ_RESULTS WHERE TABLE_FQN LIKE 'DQ_SAMPLE.%';
   ============================================================================= */

USE WAREHOUSE DQ_WH;
CREATE DATABASE IF NOT EXISTS DQ_SAMPLE COMMENT = 'Data Quality Monitor - synthetic sample data';
CREATE SCHEMA IF NOT EXISTS DQ_SAMPLE.RETAIL;
USE SCHEMA DQ_SAMPLE.RETAIL;

-- -----------------------------------------------------------------------------
-- CUSTOMERS (200,000 + ~600 duplicate rows)
-- Defects: ~2% NULL email, ~1.5% malformed email, ~3% bad phone format,
--          ~0.5% invalid country codes, ~0.2% future signup dates,
--          ~0.1% negative lifetime value, ~600 duplicated CUSTOMER_IDs
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE CUSTOMERS AS
WITH g AS (
    SELECT SEQ4() + 1 AS N,
           UNIFORM(1, 10000, RANDOM()) AS R1, UNIFORM(1, 10000, RANDOM()) AS R2,
           UNIFORM(1, 10000, RANDOM()) AS R3, UNIFORM(1, 10000, RANDOM()) AS R4
    FROM TABLE(GENERATOR(ROWCOUNT => 200000))
), n AS (
    SELECT g.*,
        ARRAY_CONSTRUCT('James','Mary','Robert','Patricia','John','Jennifer','Michael','Linda','David','Elizabeth',
                        'William','Barbara','Richard','Susan','Joseph','Jessica','Thomas','Sarah','Carlos','Priya',
                        'Wei','Aisha','Liam','Sofia','Noah')[UNIFORM(0, 24, RANDOM())]::VARCHAR AS FN,
        ARRAY_CONSTRUCT('Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis','Rodriguez','Martinez',
                        'Hernandez','Lopez','Gonzalez','Wilson','Anderson','Thomas','Taylor','Moore','Jackson','Patel',
                        'Chen','Nguyen','Kim','Muller','Rossi')[UNIFORM(0, 24, RANDOM())]::VARCHAR AS LN
    FROM g
)
SELECT
    N                                                         AS CUSTOMER_ID,
    FN                                                        AS FIRST_NAME,
    LN                                                        AS LAST_NAME,
    CASE WHEN R1 <= 200 THEN NULL
         WHEN R1 <= 350 THEN LOWER(FN) || '.' || LOWER(LN) || N || 'example.com'        -- missing @
         ELSE LOWER(FN) || '.' || LOWER(LN) || N || '@' ||
              ARRAY_CONSTRUCT('example.com','mail.com','corp.net','shop.io')[MOD(N, 4)]::VARCHAR
    END                                                       AS EMAIL,
    CASE WHEN R2 <= 300 THEN '555' || UNIFORM(1000000, 9999999, RANDOM())                 -- unformatted
         ELSE '+1-' || UNIFORM(200, 999, RANDOM()) || '-' || UNIFORM(200, 999, RANDOM()) || '-' ||
              LPAD(UNIFORM(0, 9999, RANDOM()), 4, '0')
    END                                                       AS PHONE,
    CASE WHEN R3 <= 25 THEN 'XX' WHEN R3 <= 50 THEN 'usa'
         ELSE ARRAY_CONSTRUCT('US','US','US','CA','GB','DE','FR','AU','MX','JP')[MOD(R3, 10)]::VARCHAR
    END                                                       AS COUNTRY_CODE,
    ARRAY_CONSTRUCT('Consumer','Consumer','Corporate','Small Business')[MOD(R4, 4)]::VARCHAR AS SEGMENT,
    IFF(R4 <= 20, DATEADD('day', UNIFORM(1, 400, RANDOM()), CURRENT_DATE()),
        DATEADD('day', -UNIFORM(0, 1800, RANDOM()), CURRENT_DATE()))         AS SIGNUP_DATE,
    IFF(R1 BETWEEN 9990 AND 10000, -UNIFORM(1, 500, RANDOM()),
        ROUND(UNIFORM(0, 2000000, RANDOM()) / 100, 2))::NUMBER(12,2)          AS LIFETIME_VALUE,
    DATEADD('minute', -UNIFORM(0, 720, RANDOM()), CURRENT_TIMESTAMP())::TIMESTAMP_LTZ AS UPDATED_AT
FROM n;

INSERT INTO CUSTOMERS SELECT * FROM CUSTOMERS SAMPLE (600 ROWS);   -- duplicate keys

-- -----------------------------------------------------------------------------
-- PRODUCTS (5,000)
-- Defects: ~0.6% duplicate SKUs, ~1% NULL category, ~0.4% non-positive price,
--          some rows priced below cost
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE PRODUCTS AS
WITH g AS (
    SELECT SEQ4() + 1 AS N, UNIFORM(1, 1000, RANDOM()) AS R1, UNIFORM(1, 1000, RANDOM()) AS R2
    FROM TABLE(GENERATOR(ROWCOUNT => 5000))
), p AS (
    SELECT g.*, ROUND(UNIFORM(299, 49999, RANDOM()) / 100, 2) AS PRICE,
        ARRAY_CONSTRUCT('Electronics','Home & Kitchen','Apparel','Sports','Toys','Beauty','Grocery','Outdoor')[MOD(N, 8)]::VARCHAR AS CAT
    FROM g
)
SELECT
    N                                                          AS PRODUCT_ID,
    'SKU-' || LPAD(IFF(R1 <= 6 AND N > 1, N - 1, N), 6, '0')   AS SKU,
    CAT || ' Item ' || N                                       AS PRODUCT_NAME,
    IFF(R2 <= 10, NULL, CAT)                                   AS CATEGORY,
    IFF(R1 BETWEEN 996 AND 1000, IFF(R2 > 500, 0, -PRICE), PRICE)::NUMBER(10,2) AS UNIT_PRICE,
    ROUND(PRICE * IFF(R2 >= 960, 1.15, UNIFORM(40, 75, RANDOM()) / 100), 2)::NUMBER(10,2) AS UNIT_COST,
    R2 > 50                                                    AS IS_ACTIVE,
    DATEADD('day', -UNIFORM(30, 2000, RANDOM()), CURRENT_TIMESTAMP())::TIMESTAMP_LTZ AS CREATED_AT
FROM p;

-- -----------------------------------------------------------------------------
-- ORDERS (1,500,000)
-- Defects: ~0.4% orphan CUSTOMER_ID, ~0.1% orphan PRODUCT_ID, ~0.2% invalid STATUS,
--          ~0.05% NULL ORDER_DATE, ~0.3% SHIP_DATE before ORDER_DATE,
--          ~0.2% bad QUANTITY, ~0.1% negative ORDER_AMOUNT
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE ORDERS AS
WITH g AS (
    SELECT SEQ8() + 1 AS N,
           UNIFORM(1, 10000, RANDOM()) AS R1, UNIFORM(1, 10000, RANDOM()) AS R2,
           UNIFORM(1, 10000, RANDOM()) AS R3, UNIFORM(1, 10000, RANDOM()) AS R4,
           UNIFORM(1, 10, RANDOM()) AS QTY,
           DATEADD('second', -UNIFORM(0, 365 * 86400, RANDOM()), CURRENT_TIMESTAMP()) AS ODT
    FROM TABLE(GENERATOR(ROWCOUNT => 1500000))
)
SELECT
    1000000000 + N                                                        AS ORDER_ID,
    IFF(R1 <= 40, UNIFORM(200001, 210000, RANDOM()), UNIFORM(1, 200000, RANDOM())) AS CUSTOMER_ID,
    IFF(R2 <= 10, UNIFORM(5001, 5500, RANDOM()), UNIFORM(1, 5000, RANDOM()))       AS PRODUCT_ID,
    IFF(R3 <= 5, NULL, ODT)::TIMESTAMP_LTZ                                AS ORDER_DATE,
    IFF(R3 BETWEEN 6 AND 35, DATEADD('day', -UNIFORM(1, 5, RANDOM()), ODT),
        DATEADD('hour', UNIFORM(4, 168, RANDOM()), ODT))::TIMESTAMP_LTZ   AS SHIP_DATE,
    CASE WHEN R4 <= 10 THEN 'UNKNOWN' WHEN R4 <= 20 THEN 'shipped'
         ELSE ARRAY_CONSTRUCT('DELIVERED','DELIVERED','DELIVERED','SHIPPED','SHIPPED','PENDING','CANCELLED','RETURNED')[MOD(R4, 8)]::VARCHAR
    END                                                                   AS STATUS,
    ARRAY_CONSTRUCT('Web','Mobile','Store','Marketplace')[MOD(R2, 4)]::VARCHAR AS CHANNEL,
    CASE WHEN R1 BETWEEN 9981 AND 10000 THEN 0 WHEN R1 BETWEEN 9971 AND 9980 THEN 250 ELSE QTY END AS QUANTITY,
    IFF(R2 BETWEEN 9991 AND 10000, -1, 1) * ROUND(QTY * UNIFORM(299, 49999, RANDOM()) / 100, 2)::NUMBER(12,2) AS ORDER_AMOUNT,
    DATEADD('minute', -UNIFORM(0, 180, RANDOM()), CURRENT_TIMESTAMP())::TIMESTAMP_LTZ AS LOADED_AT
FROM g;

-- -----------------------------------------------------------------------------
-- INVENTORY (50,000 = 10 warehouses x 5,000 products)
-- Defects: stale feed (last update ~2-3 days ago), ~0.5% negative on-hand qty
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE INVENTORY AS
SELECT
    FLOOR(SEQ4() / 5000) + 1                                              AS WAREHOUSE_ID,
    MOD(SEQ4(), 5000) + 1                                                 AS PRODUCT_ID,
    IFF(UNIFORM(1, 1000, RANDOM()) <= 5, -UNIFORM(1, 50, RANDOM()), UNIFORM(0, 2500, RANDOM())) AS QTY_ON_HAND,
    UNIFORM(10, 200, RANDOM())                                            AS REORDER_POINT,
    DATEADD('minute', -UNIFORM(50 * 60, 80 * 60, RANDOM()), CURRENT_TIMESTAMP())::TIMESTAMP_LTZ AS LAST_UPDATED
FROM TABLE(GENERATOR(ROWCOUNT => 50000));

-- -----------------------------------------------------------------------------
-- Register sample rules
-- -----------------------------------------------------------------------------
DELETE FROM DQ_FRAMEWORK.CORE.DQ_RULES   WHERE DATABASE_NAME = 'DQ_SAMPLE';
DELETE FROM DQ_FRAMEWORK.CORE.DQ_RESULTS WHERE TABLE_FQN LIKE 'DQ_SAMPLE.%';

INSERT INTO DQ_FRAMEWORK.CORE.DQ_RULES
    (RULE_NAME, DESCRIPTION, DATABASE_NAME, SCHEMA_NAME, TABLE_NAME, COLUMN_NAME, RULE_TYPE,
     RULE_PARAMS, ROW_FILTER, THRESHOLD_PCT, SEVERITY, DIMENSION, OWNER)
SELECT $1, $2, 'DQ_SAMPLE', 'RETAIL', $3, $4, $5, PARSE_JSON($6), $7, $8, $9, $10, $11
FROM VALUES
 -- CUSTOMERS
 ('Customer ID present',        'Every customer must have an ID',                 'CUSTOMERS','CUSTOMER_ID','NOT_NULL',       '{}', NULL, 0,   'CRITICAL','Completeness','CRM Team'),
 ('Customer ID unique',         'Primary key must not repeat',                    'CUSTOMERS','CUSTOMER_ID','UNIQUE',         '{}', NULL, 0,   'CRITICAL','Uniqueness',  'CRM Team'),
 ('Customer email populated',   'Email needed for marketing and receipts',        'CUSTOMERS','EMAIL',      'NOT_NULL',       '{}', NULL, 1,   'HIGH',    'Completeness','CRM Team'),
 ('Customer email format',      'Email must look like user@domain.tld',           'CUSTOMERS','EMAIL',      'REGEX',          '{"pattern":"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\\\.[A-Za-z]{2,}$"}', NULL, 2, 'MEDIUM','Validity','CRM Team'),
 ('Customer phone format',      'Phone must be E.164-style +1-NNN-NNN-NNNN',      'CUSTOMERS','PHONE',      'REGEX',          '{"pattern":"^\\\\+1-[0-9]{3}-[0-9]{3}-[0-9]{4}$"}', NULL, 5, 'LOW','Validity','CRM Team'),
 ('Customer country code valid','ISO country codes from the supported list',      'CUSTOMERS','COUNTRY_CODE','ACCEPTED_VALUES','{"values":["US","CA","GB","DE","FR","AU","MX","JP"]}', NULL, 0, 'MEDIUM','Validity','CRM Team'),
 ('Customer segment valid',     'Segment must be a known value',                  'CUSTOMERS','SEGMENT',    'ACCEPTED_VALUES','{"values":["Consumer","Corporate","Small Business"]}', NULL, 0, 'LOW','Validity','CRM Team'),
 ('Signup date not in future',  'Signup cannot be after today',                   'CUSTOMERS','SIGNUP_DATE','CUSTOM_SQL',     '{"failure_condition":"SIGNUP_DATE > CURRENT_DATE()"}', NULL, 0, 'HIGH','Consistency','CRM Team'),
 ('Lifetime value non-negative','LTV must be >= 0',                               'CUSTOMERS','LIFETIME_VALUE','RANGE',       '{"min":0}', NULL, 0.5, 'MEDIUM','Validity','Finance'),
 ('Customers refreshed daily',  'CRM sync must land within 24h',                  'CUSTOMERS','UPDATED_AT', 'FRESHNESS',      '{"max_age_hours":24}', NULL, 0, 'HIGH','Timeliness','Data Engineering'),
 ('Customer volume',            'Expect at least 100K customers',                 'CUSTOMERS',NULL,         'ROW_COUNT',      '{"min":100000}', NULL, 0, 'MEDIUM','Volume','Data Engineering'),
 -- PRODUCTS
 ('Product ID unique',          'Primary key must not repeat',                    'PRODUCTS','PRODUCT_ID', 'UNIQUE',          '{}', NULL, 0, 'CRITICAL','Uniqueness','Merchandising'),
 ('SKU unique',                 'Each SKU maps to exactly one product',           'PRODUCTS','SKU',        'UNIQUE',          '{}', NULL, 0, 'HIGH','Uniqueness','Merchandising'),
 ('Product category populated', 'Category drives reporting hierarchy',            'PRODUCTS','CATEGORY',   'NOT_NULL',        '{}', NULL, 2, 'MEDIUM','Completeness','Merchandising'),
 ('Unit price positive',        'Active products must have a positive price',     'PRODUCTS','UNIT_PRICE', 'RANGE',           '{"min":0.01}', 'IS_ACTIVE', 0, 'HIGH','Validity','Merchandising'),
 ('Price above cost',           'Selling below cost indicates a pricing error',   'PRODUCTS','UNIT_PRICE', 'CUSTOM_SQL',      '{"failure_condition":"UNIT_PRICE < UNIT_COST"}', 'UNIT_PRICE > 0', 10, 'LOW','Consistency','Finance'),
 -- ORDERS
 ('Order ID unique',            'Primary key must not repeat',                    'ORDERS','ORDER_ID',     'UNIQUE',          '{}', NULL, 0, 'CRITICAL','Uniqueness','Order Mgmt'),
 ('Order has valid customer',   'CUSTOMER_ID must exist in CUSTOMERS',            'ORDERS','CUSTOMER_ID',  'REFERENTIAL',     '{"ref_table":"DQ_SAMPLE.RETAIL.CUSTOMERS","ref_column":"CUSTOMER_ID"}', NULL, 0.1, 'CRITICAL','Consistency','Order Mgmt'),
 ('Order has valid product',    'PRODUCT_ID must exist in PRODUCTS',              'ORDERS','PRODUCT_ID',   'REFERENTIAL',     '{"ref_table":"DQ_SAMPLE.RETAIL.PRODUCTS","ref_column":"PRODUCT_ID"}', NULL, 0.5, 'HIGH','Consistency','Order Mgmt'),
 ('Order status valid',         'Status must be an approved lifecycle value',     'ORDERS','STATUS',       'ACCEPTED_VALUES', '{"values":["PENDING","SHIPPED","DELIVERED","CANCELLED","RETURNED"]}', NULL, 0, 'HIGH','Validity','Order Mgmt'),
 ('Order date populated',       'Every order needs an order timestamp',           'ORDERS','ORDER_DATE',   'NOT_NULL',        '{}', NULL, 0.1, 'CRITICAL','Completeness','Order Mgmt'),
 ('Ship date after order date', 'Cannot ship before the order was placed',        'ORDERS','SHIP_DATE',    'CUSTOM_SQL',      '{"failure_condition":"SHIP_DATE < ORDER_DATE"}', NULL, 0.1, 'MEDIUM','Consistency','Fulfillment'),
 ('Order quantity in range',    'Quantity between 1 and 100',                     'ORDERS','QUANTITY',     'RANGE',           '{"min":1,"max":100}', NULL, 0.5, 'MEDIUM','Validity','Order Mgmt'),
 ('Order amount non-negative',  'Refunds are tracked separately; amounts >= 0',   'ORDERS','ORDER_AMOUNT', 'RANGE',           '{"min":0}', 'STATUS <> ''CANCELLED''', 0, 'HIGH','Validity','Finance'),
 ('Orders loaded hourly',       'Order pipeline must land within 6h',             'ORDERS','LOADED_AT',    'FRESHNESS',       '{"max_age_hours":6}', NULL, 0, 'CRITICAL','Timeliness','Data Engineering'),
 ('Order volume',               'Expect 1M-5M orders in the table',               'ORDERS',NULL,           'ROW_COUNT',       '{"min":1000000,"max":5000000}', NULL, 0, 'MEDIUM','Volume','Data Engineering'),
 -- INVENTORY
 ('Inventory refreshed daily',  'WMS feed must land within 24h',                  'INVENTORY','LAST_UPDATED','FRESHNESS',     '{"max_age_hours":24}', NULL, 0, 'CRITICAL','Timeliness','Supply Chain'),
 ('On-hand quantity >= 0',      'Negative stock indicates a sync error',          'INVENTORY','QTY_ON_HAND', 'RANGE',         '{"min":0}', NULL, 1, 'MEDIUM','Validity','Supply Chain'),
 ('Inventory key unique',       'One row per warehouse + product',                'INVENTORY','WAREHOUSE_ID,PRODUCT_ID','UNIQUE','{}', NULL, 0, 'HIGH','Uniqueness','Supply Chain'),
 ('Inventory product valid',    'PRODUCT_ID must exist in PRODUCTS',              'INVENTORY','PRODUCT_ID',  'REFERENTIAL',   '{"ref_table":"DQ_SAMPLE.RETAIL.PRODUCTS","ref_column":"PRODUCT_ID"}', NULL, 0, 'LOW','Consistency','Supply Chain');

-- -----------------------------------------------------------------------------
-- Run the checks for real (this becomes the "latest" run)
-- -----------------------------------------------------------------------------
CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS('DQ_SAMPLE.RETAIL.CUSTOMERS');
CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS('DQ_SAMPLE.RETAIL.PRODUCTS');
CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS('DQ_SAMPLE.RETAIL.ORDERS');
CALL DQ_FRAMEWORK.CORE.RUN_DQ_CHECKS('DQ_SAMPLE.RETAIL.INVENTORY');

-- -----------------------------------------------------------------------------
-- Backfill 30 days of simulated history derived from the real latest results:
-- noisy failure rates, an ORDERS pipeline incident 12-14 days ago, and slow
-- table growth. Binary checks (FRESHNESS / ROW_COUNT) fail occasionally, and
-- currently-failing ones started failing 2 days ago.
-- -----------------------------------------------------------------------------
INSERT INTO DQ_FRAMEWORK.CORE.DQ_RESULTS
    (RUN_ID, RUN_TS, RULE_ID, RULE_NAME, TABLE_FQN, COLUMN_NAME, RULE_TYPE, DIMENSION, SEVERITY,
     TOTAL_ROWS, FAILED_ROWS, FAILED_PCT, THRESHOLD_PCT, STATUS, ERROR_MESSAGE, DURATION_MS,
     CHECK_SQL, SAMPLE_SQL, TRIGGERED_BY)
WITH days AS (
    SELECT SEQ4() + 1 AS D FROM TABLE(GENERATOR(ROWCOUNT => 30))
), base AS (
    SELECT RUN_TS, RULE_ID, RULE_NAME, TABLE_FQN, COLUMN_NAME, RULE_TYPE, DIMENSION, SEVERITY,
           TOTAL_ROWS, FAILED_PCT, THRESHOLD_PCT, STATUS, DURATION_MS, CHECK_SQL, SAMPLE_SQL
    FROM DQ_FRAMEWORK.CORE.V_LATEST_RESULTS
    WHERE TABLE_FQN LIKE 'DQ_SAMPLE.%'
), sim AS (
    SELECT b.*, d.D,
           ABS(HASH(b.RULE_ID, d.D)) % 1000 / 1000.0                          AS RND,
           b.RULE_TYPE IN ('FRESHNESS', 'ROW_COUNT')                            AS IS_BINARY,
           IFF(b.TABLE_FQN LIKE '%.ORDERS' AND d.D BETWEEN 12 AND 14, 6, 1)     AS INCIDENT
    FROM base b CROSS JOIN days d
), calc AS (
    SELECT sim.*,
           IFF(IS_BINARY, 1, GREATEST(1, ROUND(TOTAL_ROWS * (1 - D * 0.004))))  AS S_TOTAL,
           CASE WHEN IS_BINARY THEN IFF((STATUS = 'FAIL' AND D <= 2) OR RND < 0.04 OR INCIDENT > 1, 100, 0)
                WHEN RND < 0.2 AND D > 5 THEN 0
                ELSE LEAST(100, (FAILED_PCT + IFF(INCIDENT > 1, 0.3, 0)) * (0.3 + RND * 0.9) * INCIDENT)
           END                                                                 AS S_PCT
    FROM sim
)
SELECT MD5('dq-backfill-' || D), DATEADD('day', -D, RUN_TS), RULE_ID, RULE_NAME, TABLE_FQN, COLUMN_NAME,
       RULE_TYPE, DIMENSION, SEVERITY, S_TOTAL, ROUND(S_TOTAL * S_PCT / 100), ROUND(S_PCT, 4), THRESHOLD_PCT,
       IFF(S_PCT > THRESHOLD_PCT, 'FAIL', 'PASS'), NULL, ROUND(DURATION_MS * (0.7 + RND * 0.6)),
       CHECK_SQL, SAMPLE_SQL, 'BACKFILL'
FROM calc;

-- Quick look
SELECT * FROM DQ_FRAMEWORK.CORE.V_TABLE_HEALTH ORDER BY DQ_SCORE;
