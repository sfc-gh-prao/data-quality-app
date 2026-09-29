"""Unit tests for the rule compiler embedded in sql/02_engine.sql.

Run:  python -m pytest tests/   (no Snowflake connection required)
"""

import pathlib
import re
import sys
import types

import pytest

ENGINE = pathlib.Path(__file__).resolve().parents[1] / "sql" / "02_engine.sql"


@pytest.fixture(scope="module")
def eng():
    body = re.search(r"AS\s*\$\$(.*?)\$\$", ENGINE.read_text(), re.S).group(1)
    # Stub snowpark types so the handler module imports without the Snowpark package.
    if "snowflake.snowpark.types" not in sys.modules:
        stub = types.ModuleType("snowflake.snowpark.types")
        for n in ["DoubleType", "LongType", "StringType", "StructField", "StructType", "TimestampType", "TimestampTimeZone"]:
            setattr(stub, n, type(n, (), {"__init__": lambda self, *a, **k: None, "LTZ": None}))
        sys.modules.update({"snowflake": types.ModuleType("snowflake"),
                            "snowflake.snowpark": types.ModuleType("snowflake.snowpark"),
                            "snowflake.snowpark.types": stub})
    mod = types.ModuleType("engine")
    exec(body, mod.__dict__)
    return mod


def rule(**kw):
    base = dict(DATABASE_NAME="db", SCHEMA_NAME="sch", TABLE_NAME="t", COLUMN_NAME="c",
                RULE_TYPE="NOT_NULL", RULE_PARAMS={}, ROW_FILTER=None)
    base.update(kw)
    return base


def test_identifiers_quoted_and_uppercased(eng):
    check, _ = eng.build_sql(rule())
    assert '"DB"."SCH"."T"' in check and '"C" IS NULL' in check


def test_mixed_case_identifier_preserved(eng):
    assert eng.q_ident("My Col") == '"My Col"'
    assert eng.q_ident('we"ird') == '"we""ird"'


@pytest.mark.parametrize("rtype,params,needle", [
    ("UNIQUE", {}, "HAVING COUNT(*) > 1"),
    ("ACCEPTED_VALUES", {"values": ["A", "O'B"]}, "NOT IN ('A', 'O''B')"),
    ("RANGE", {"min": 0, "max": 5}, '"C" < 0.0 OR "C" > 5.0'),
    ("REGEX", {"pattern": r"^\d+$"}, "NOT REGEXP_LIKE(\"C\"::VARCHAR, '^\\\\d+$')"),
    ("FRESHNESS", {"max_age_hours": 2}, "> 120"),
    ("ROW_COUNT", {"min": 10}, "COUNT(*) < 10"),
    ("REFERENTIAL", {"ref_table": "a.b.p", "ref_column": "id"}, '"A"."B"."P"'),
    ("CUSTOM_SQL", {"failure_condition": "X < Y"}, "COUNT_IF((X < Y))"),
])
def test_rule_types(eng, rtype, params, needle):
    check, sample = eng.build_sql(rule(RULE_TYPE=rtype, RULE_PARAMS=params))
    assert needle in check
    assert "TOTAL_ROWS" in check and "FAILED_ROWS" in check
    assert sample.startswith("SELECT")


def test_composite_unique(eng):
    check, sample = eng.build_sql(rule(RULE_TYPE="UNIQUE", COLUMN_NAME="a, b"))
    assert 'GROUP BY "A", "B"' in check and 'PARTITION BY "A", "B"' in sample


def test_row_filter_applied(eng):
    check, sample = eng.build_sql(rule(ROW_FILTER="STATUS = 'X'"))
    assert "WHERE (STATUS = 'X')" in check and "WHERE (STATUS = 'X')" in sample


def test_params_as_json_string(eng):
    check, _ = eng.build_sql(rule(RULE_TYPE="RANGE", RULE_PARAMS='{"min": 1}'))
    assert '"C" < 1.0' in check


@pytest.mark.parametrize("r", [
    rule(RULE_TYPE="NOPE"),
    rule(RULE_TYPE="RANGE", RULE_PARAMS={}),
    rule(RULE_TYPE="ACCEPTED_VALUES", RULE_PARAMS={"values": []}),
    rule(RULE_TYPE="NOT_NULL", COLUMN_NAME=None),
    rule(RULE_TYPE="REFERENTIAL", RULE_PARAMS={"ref_table": "a.b.c"}),
])
def test_invalid_rules_raise(eng, r):
    with pytest.raises(ValueError):
        eng.build_sql(r)
