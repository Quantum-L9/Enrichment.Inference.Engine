"""tests/e2e is opt-in: its modules are collected only when EIE_E2E=1.

The rail (Gate + EIE + Redis + Postgres in Docker) does not exist on a normal
unit run, so `pytest tests/` must neither fail on it nor report it as passed or
skipped. Without the flag the modules are not collected at all; with it, a
missing rail is a failure. `run_converge_e2e.sh` sets the flag.
"""

from __future__ import annotations

import os

collect_ignore_glob = [] if os.environ.get("EIE_E2E", "").strip() == "1" else ["test_*.py"]
