"""Live smoke test: render every page against a real Snowflake account.

Run:  SNOWFLAKE_DEFAULT_CONNECTION_NAME=<conn> python tests/live_check.py
"""

import pathlib
import sys

from streamlit.testing.v1 import AppTest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

failures = 0
for page in ["overview", "table_health", "run_history", "rules", "native_dmfs"]:
    at = AppTest.from_file(str(APP / "app_pages" / f"{page}.py"), default_timeout=120).run()
    errs = [e.value for e in at.exception] + [e.value for e in at.error]
    kpis = sum(1 for m in at.markdown if "dq-card" in m.value)
    status = "FAIL" if at.exception else "OK"
    failures += bool(at.exception)
    print(f"{status:4} {page:13} kpi_cards={kpis} dataframes={len(at.dataframe)} "
          f"charts={len(at.get('arrow_vega_lite_chart'))} messages={errs[:2]}")
sys.exit(1 if failures else 0)
