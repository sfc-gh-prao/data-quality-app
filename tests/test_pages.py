"""Render smoke test for every app page using synthetic framework data.

Run:  python -m pytest tests/   (no Snowflake connection required)
"""

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))


def _results():
    rng = np.random.default_rng(7)
    now = datetime.now(timezone.utc)
    rows = []
    specs = [("DB.S.ORDERS", "CRITICAL", "Uniqueness", "UNIQUE"), ("DB.S.ORDERS", "HIGH", "Validity", "RANGE"),
             ("DB.S.CUSTOMERS", "MEDIUM", "Completeness", "NOT_NULL"), ("DB.S.CUSTOMERS", "LOW", "Timeliness", "FRESHNESS")]
    for d in range(10):
        for i, (t, sev, dim, rt) in enumerate(specs):
            pct = float(rng.uniform(0, 1.3))
            status = "ERROR" if (i == 3 and d == 0) else ("FAIL" if pct > 1 else "PASS")
            rows.append(dict(RESULT_ID=len(rows), RUN_ID=f"r{d}", RUN_TS=now - timedelta(days=d), RULE_ID=i + 1,
                             RULE_NAME=f"Rule {i + 1}", TABLE_FQN=t, COLUMN_NAME="C", RULE_TYPE=rt, DIMENSION=dim,
                             SEVERITY=sev, TOTAL_ROWS=1000, FAILED_ROWS=int(pct * 10), FAILED_PCT=pct, THRESHOLD_PCT=1.0,
                             STATUS=status, ERROR_MESSAGE="boom" if status == "ERROR" else None, DURATION_MS=120,
                             CHECK_SQL="SELECT 1", SAMPLE_SQL="SELECT 1", TRIGGERED_BY="SCHEDULE",
                             WEIGHT={"CRITICAL": 8, "HIGH": 4, "MEDIUM": 2, "LOW": 1}[sev], OWNER="Team", DESCRIPTION="d"))
    return pd.DataFrame(rows)


def fake_query(sql, params=None):
    r = _results()
    r["RUN_DATE"] = r["RUN_TS"].dt.date
    latest = r[r["RUN_ID"] == "r0"]
    if "V_TABLE_HEALTH" in sql:
        g = latest.groupby("TABLE_FQN")
        return pd.DataFrame({"CHECKS": g.size(), "PASSED": g.apply(lambda x: (x.STATUS == "PASS").sum()),
                             "FAILED": g.apply(lambda x: (x.STATUS == "FAIL").sum()),
                             "ERRORS": g.apply(lambda x: (x.STATUS == "ERROR").sum()), "CRITICAL_ISSUES": 1,
                             "DQ_SCORE": 87.5, "LAST_RUN_TS": g.RUN_TS.max()}).reset_index()
    if "V_DAILY_TABLE_SCORE" in sql:
        return r.groupby(["RUN_DATE", "TABLE_FQN"]).size().reset_index(name="CHECKS").assign(DQ_SCORE=90.0, FAILED=1)
    if "V_LATEST_RESULTS" in sql and "DQ_RULES" not in sql:
        return latest.copy()
    if "V_DAILY_RESULTS" in sql and "GROUP BY" in sql:
        return r.groupby("RUN_DATE").size().reset_index(name="CHECKS").assign(DQ_SCORE=91.0, ISSUES=1)
    if "V_DAILY_RESULTS" in sql:
        return r[r.RULE_ID == 1][["RUN_DATE", "FAILED_PCT", "THRESHOLD_PCT", "STATUS", "FAILED_ROWS"]]
    if "DISTINCT TABLE_FQN" in sql:
        return pd.DataFrame({"TABLE_FQN": r.TABLE_FQN.unique()})
    if "FROM DQ_FRAMEWORK.CORE.DQ_RESULTS" in sql:
        return r.drop(columns=["RUN_DATE"])
    if "FROM DQ_FRAMEWORK.CORE.DQ_RULES r" in sql:
        l = latest.copy()
        return l.assign(IS_ACTIVE=True, RULE_PARAMS="{}", ROW_FILTER=None, UPDATED_AT=l.RUN_TS,
                        LAST_STATUS=l.STATUS, DATABASE_NAME="DB", SCHEMA_NAME="S",
                        TABLE_NAME=l.TABLE_FQN.str.split(".").str[-1])
    if "SHOW TERSE DATABASES" in sql:
        return pd.DataFrame({"NAME": ["DB"]})
    if "DATA_QUALITY_MONITORING_RESULTS" in sql:
        return pd.DataFrame({"MEASUREMENT_TIME": r.RUN_TS, "TABLE_FQN": r.TABLE_FQN, "METRIC": "SNOWFLAKE.CORE.NULL_COUNT",
                             "ARGUMENTS": "C", "VALUE": r.FAILED_ROWS})
    return pd.DataFrame({"X": [1]})


@pytest.fixture(autouse=True)
def patch_db(monkeypatch):
    from lib import db
    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(db, "require_framework", lambda: None)


@pytest.mark.parametrize("page", ["overview", "table_health", "run_history", "rules", "native_dmfs"])
def test_page_renders(page):
    at = AppTest.from_file(str(APP / "app_pages" / f"{page}.py"), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


def test_table_health_drilldown():
    at = AppTest.from_file(str(APP / "app_pages" / "table_health.py"), default_timeout=30)
    at.run()
    assert not at.exception
    assert any("DQ Score" in m.value for m in at.markdown)
