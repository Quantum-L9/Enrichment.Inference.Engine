# CI canary fixtures — DO NOT MERGE

Disposable witnesses for campaign `L9-CI-UNIFIED-LEVERAGE-2026-11-20-v1.7.1`,
lane CANARY-EIE. These files are scan input only. Nothing imports them, and
pytest does not collect them (`pytest.ini` collects `tests/test_*.py`).

| File | Rule it targets | Rule owner | Expected |
|---|---|---|---|
| `central_rule_bad.py` | `l9.baseline.python.subprocess-shell-true` (SDK) and the `p/python` subprocess shell rule | Core / SDK (central) | fires in `Analyze (central Core)`, blocking |
| `central_rule_good.py` | same | Core / SDK | no finding |
| `local_rule_bad.py` | `no-datetime-utcnow` in `.semgrep/semgrep-rules.yaml` | EIE | fires in EIE `Semgrep Policy Check` |
| `local_rule_good.py` | same | EIE | no finding |
| `override_attempt.py` | central rule, suppressed inline with `nosemgrep` | consumer attempt | must not remove the central finding |

`tests/canary/central_rule_bad_in_tests.py` is a scope witness: the same
central known-bad construct placed under `tests/`.
