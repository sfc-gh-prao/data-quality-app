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
    now = datetime.now(timezone.utc)
    if "GROUP BY STATUS" in sql:
        return pd.DataFrame({"STATUS": ["SENT", "SKIPPED"], "N": [1, 1], "AT": [now, now]})
    if "DQ_ALERT_CONFIG" in sql:
        return pd.DataFrame({"ALERT_ID": [1], "IS_ENABLED": [True], "ALERT_NAME": ["Email - CRITICAL"],
                             "CHANNEL_TYPE": ["EMAIL"], "RECIPIENTS": ["dq@example.com"], "SEVERITY_FILTER": ["CRITICAL"],
                             "SCOPE_TABLES": [None], "NOTIFY_MODE": ["ALL_FAILURES"], "MIN_SCORE": [None]})
    if "DQ_ALERT_HISTORY" in sql:
        return pd.DataFrame({"HISTORY_ID": [1], "SENT_AT": [now], "ALERT_ID": [1], "ALERT_NAME": ["Email - CRITICAL"],
                             "CHANNEL_TYPE": ["EMAIL"], "STATUS": ["SENT"], "FAILURES_COUNT": [2],
                             "TABLES_AFFECTED": ["DB.S.ORDERS"], "MESSAGE_PREVIEW": ["msg"], "ERROR_MESSAGE": [None]})
    if "DQ_SETTINGS" in sql:
        return pd.DataFrame({"SETTING_KEY": ["APP_URL"], "SETTING_VALUE": [""]})
    if "SHOW NOTIFICATION INTEGRATIONS" in sql:
        return pd.DataFrame({"NAME": ["DQ_EMAIL_INTEGRATION"], "TYPE": ["EMAIL"]})
    if "DESCRIBE INTEGRATION" in sql:
        return pd.DataFrame({"PROPERTY": ["ALLOWED_RECIPIENTS"], "PROPERTY_VALUE": ["dq@example.com"]})
    if "SHOW TASKS" in sql:
        return pd.DataFrame({"NAME": ["DQ_DAILY_RUN", "DQ_NOTIFY"], "STATE": ["suspended", "started"],
                             "SCHEDULE": ["USING CRON 0 6 * * * UTC", None]})
    if "TASK_HISTORY" in sql:
        return pd.DataFrame()
    if "AS T, " in sql:  # Alerting scope picker
        return pd.DataFrame({"T": ["DB.S.ORDERS"], "S": ["DB.S.*"]})
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
    monkeypatch.setattr(db, "run_uncached", fake_query)
    monkeypatch.setattr(db, "require_framework", lambda: None)
    monkeypatch.setattr(db, "_in_sis", lambda: False)  # local = admin


@pytest.mark.parametrize("page", ["overview", "table_health", "run_history", "rules", "native_dmfs",
                                  "schema_explorer", "alerting"])
def test_page_renders(page):
    at = AppTest.from_file(str(APP / "app_pages" / f"{page}.py"), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


def test_table_health_drilldown():
    at = AppTest.from_file(str(APP / "app_pages" / "table_health.py"), default_timeout=30)
    at.run()
    assert not at.exception
    assert any("DQ Score" in m.value for m in at.markdown)


def test_read_only_viewer_blocks_writes(monkeypatch):
    from lib import db
    monkeypatch.setattr(db, "is_admin", lambda: False)
    with pytest.raises(PermissionError):
        db.execute("DELETE FROM X")
    with pytest.raises(PermissionError):
        db.execute_many("DELETE FROM X", [()])


@pytest.mark.parametrize("page", ["schema_explorer", "alerting", "rules", "table_health"])
def test_pages_render_read_only(monkeypatch, page):
    from lib import db
    monkeypatch.setattr(db, "is_admin", lambda: False)
    at = AppTest.from_file(str(APP / "app_pages" / f"{page}.py"), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    if page != "table_health":  # its lock message only appears after a check is selected
        assert any("read-only" in i.value or "admins" in i.value for i in at.info)
