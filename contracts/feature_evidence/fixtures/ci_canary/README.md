# CI canary fixtures — DO NOT MERGE

Disposable witnesses for campaign `L9-CI-UNIFIED-LEVERAGE-2026-11-20-v1.7.1`,
lane CANARY-EIE. These files are scan input only. Nothing imports them, and
pytest does not collect them (`pytest.ini` collects `tests/test_*.py`).

| File | Rule it targets | Rule owner | Expected |
|---|---|---|---|
| `central_rule_bad.py` | `l9.baseline.python.subprocess-shell-true` (SDK); up to SDK 47ae3ac5 also the `p/python` duplicate, superseded since SDK #143 | Core / SDK (central) | fires in `Analyze (central Core)`, blocking |
| `central_rule_good.py` | same | Core / SDK | no finding |
| `local_rule_bad.py` | `no-datetime-utcnow` in `.semgrep/semgrep-rules.yaml` | EIE | fires in EIE `Semgrep Policy Check` |
| `local_rule_good.py` | same | EIE | no finding |
| `override_attempt.py` | central rule, suppressed inline with `nosemgrep` | consumer attempt | must not remove the central finding |

`tests/canary/central_rule_bad_in_tests.py` is a scope witness: the same
central known-bad construct placed under `tests/`. Up to SDK 47ae3ac5 the
central scan-scope policy excluded `tests/` (run 1: not scanned). Since SDK #134,
`tests/` is in the required scope, so the L9 rule is expected to fire there and
block.

## Evidence

Run 1. Head `7c836078`, org-ci Core `2ff05a03`, SDK `47ae3ac5`, central mode
blocking. Full record: PR #378 comment 7058473821.

| Gate | Verdict | Run / job |
|---|---|---|
| 2: central rule fires and blocks | PASS. `central_rule_bad.py:8`, both rules, blocking | 44874376220 / 136755713865 |
| 3: consumer cannot weaken central coverage | PASS. `override_attempt.py:14` still blocks (`nosem: disabled`); `tests/` is excluded by central scan-scope policy 1.1.0, not by the consumer | 44874376220 / 136755713865 |
| EIE local Semgrep | FAIL as designed. `local_rule_bad.py` only, run twice (duplicate job) | 44874376340 / 136755763813; 44874376228 / 136755781181 |

Run 2 is due after PIN-ADVANCE-3 (Core #324 `12befe6`, SDK `4a887396`,
CENTRAL-DIFF and the waiver ledger live). Its result is recorded on PR #378.
