"""Snowflake access layer.

Works both in Streamlit in Snowflake (embedded identity) and locally
(`SNOWFLAKE_DEFAULT_CONNECTION_NAME=<conn> streamlit run streamlit_app.py`).
"""

import json
import os
import re
from decimal import Decimal

import pandas as pd
import streamlit as st


def _setting(name: str, default: str) -> str:
    """Read a setting from st.secrets, then the environment, then a default."""
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # no secrets file (typical in SiS)
        pass
    return os.getenv(name, default)


# Where DQ_RULES / DQ_RESULTS / views live. Change here, in secrets, or via env var.
FRAMEWORK = _setting("DQ_FRAMEWORK_SCHEMA", "DQ_FRAMEWORK.CORE")
if not re.fullmatch(r'[A-Za-z0-9_$."]+', FRAMEWORK):
    raise ValueError(f"Invalid DQ_FRAMEWORK_SCHEMA: {FRAMEWORK!r}")

APP_TITLE = _setting("DQ_APP_TITLE", "Data Quality Monitor")


@st.cache_resource(show_spinner=False)
def get_conn():
    return st.connection(
        "snowflake", ttl=os.getenv("SNOWFLAKE_CONNECTION_TTL")
    )


def _run(sql: str, params=None) -> pd.DataFrame:
    # Raw cursor instead of conn.query(): works regardless of Arrow/JSON result format.
    with get_conn().raw_connection.cursor() as cur:
        cur.execute(sql, params or None)
        if cur.description is None:
            return pd.DataFrame()
        cols = [c[0].upper() for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    # NUMBER columns arrive as decimal.Decimal; convert so pandas math/charts behave.
    for c in df.columns:
        first = df[c].dropna().head(1)
        if len(first) and isinstance(first.iloc[0], Decimal):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def query(sql: str, params: tuple | None = None) -> pd.DataFrame:
    """Cached read. Use ? (qmark) placeholders for params — never format values into SQL."""
    return _run(sql, params)


def execute(sql: str, params: tuple | None = None) -> pd.DataFrame:
    """Uncached statement (writes, CALLs). Clears read caches afterwards."""
    df = _run(sql, params)
    st.cache_data.clear()
    return df


def execute_many(sql: str, rows: list[tuple]) -> int:
    """Run one parameterized statement per row, then clear caches once."""
    with get_conn().raw_connection.cursor() as cur:
        for r in rows:
            cur.execute(sql, r)
    st.cache_data.clear()
    return len(rows)


def fq(obj: str) -> str:
    """Fully qualify a framework object, e.g. fq('DQ_RULES')."""
    return f"{FRAMEWORK}.{obj}"


def q_ident(name: str) -> str:
    """Safely quote a Snowflake identifier for use in SQL text."""
    name = str(name)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        name = name.upper()
    return '"' + name.replace('"', '""') + '"'


def require_framework():
    """Stop the page with setup instructions if the framework objects are missing."""
    try:
        query(f"SELECT 1 FROM {fq('V_TABLE_HEALTH')} LIMIT 1")
    except Exception as e:
        st.error(f"Data Quality framework not found at `{FRAMEWORK}`.")
        st.info(
            "Run `sql/01_setup.sql` and `sql/02_engine.sql` (and optionally `sql/03_sample_data.sql`), "
            "or point the app at your framework schema with the `DQ_FRAMEWORK_SCHEMA` setting.\n\n"
            f"Details: {e}"
        )
        st.stop()


def run_checks(table_fqn: str | None = None, rule_id: int | None = None) -> dict:
    df = execute(f"CALL {fq('RUN_DQ_CHECKS')}(?, ?, 'MANUAL')", (table_fqn, rule_id))
    val = df.iloc[0, 0] if not df.empty else "{}"
    return json.loads(val) if isinstance(val, str) else dict(val)
