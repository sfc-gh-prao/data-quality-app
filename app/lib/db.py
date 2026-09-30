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

# Viewers holding this role (via their DEFAULT_ROLE hierarchy) may write: rules, runs, profiling, alerts.
ADMIN_ROLE = _setting("DQ_ADMIN_ROLE", "DQ_ADMIN")
if not re.fullmatch(r"[A-Za-z0-9_$]+", ADMIN_ROLE):
    raise ValueError(f"Invalid DQ_ADMIN_ROLE: {ADMIN_ROLE!r}")


def _in_sis() -> bool:
    return os.path.exists("/snowflake/session/token")


def init_viewer():
    """Resolve who is viewing and whether they are an admin. Call once at the top of the main script.

    The app runs with its owner's rights, so admin status comes from a restricted caller's-rights
    connection (the viewer's DEFAULT_ROLE). Locally there is no viewer, so the developer is admin.
    If the check can't run inside Snowflake, the viewer is treated as read-only (fail closed).
    """
    if "dq_is_admin" in st.session_state:
        return
    if not _in_sis():
        st.session_state.update(dq_is_admin=True, dq_viewer="local developer", dq_viewer_note=None)
        return
    try:
        conn = st.connection("snowflake-callers-rights")
        with conn.raw_connection.cursor() as cur:
            cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), IS_ROLE_IN_SESSION(?)", (ADMIN_ROLE,))
            user, role, is_admin = cur.fetchone()
        st.session_state.update(dq_is_admin=bool(is_admin), dq_viewer=str(user), dq_viewer_note=None)
    except Exception as e:
        st.session_state.update(dq_is_admin=False, dq_viewer="unknown",
                                dq_viewer_note=f"Could not verify your role, so the app is read-only: {e}")


def is_admin() -> bool:
    if st.session_state.get("dq_preview_readonly"):
        return False
    return bool(st.session_state.get("dq_is_admin", not _in_sis()))


def _require_admin():
    if not is_admin():
        raise PermissionError(f"Read-only: this action requires the {ADMIN_ROLE} role in your default role.")


def read_only_notice(what: str = "make changes"):
    """Show a consistent banner for non-admin viewers. Returns True when the viewer is read-only."""
    if is_admin():
        return False
    st.info(f"You're viewing read-only. Ask for the **{ADMIN_ROLE}** role (as part of your default role) to {what}.",
            icon=":material/lock:")
    return True


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
    """Uncached statement (writes, CALLs). Admins only. Clears read caches afterwards."""
    _require_admin()
    df = _run(sql, params)
    st.cache_data.clear()
    return df


def run_uncached(sql: str, params: tuple | None = None) -> pd.DataFrame:
    """Uncached read that leaves caches intact (profiling, previews, status checks)."""
    return _run(sql, params)


def execute_many(sql: str, rows: list[tuple]) -> int:
    """Run one parameterized statement per row, then clear caches once. Admins only."""
    _require_admin()
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
