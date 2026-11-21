# EIE org-CI cutover canary — DO NOT MERGE

Negative witnesses for EIE org-CI cutover Phase 2 (PRs #380, #382, #383, #384,
merged into this branch). Each file under this directory is a known-bad or
known-good case; the required `Analyze (central Core)` check must fail on each
known-bad case with the named rule, and stay silent on the known-good one.

| File | Expected org-CI result |
|---|---|
| `gitleaks_word_bad.py` | `security / security` gitleaks: finding `openai-api-key` (a synthetic key containing "test"; silent under the former global allowlist) |
| `consumer_no_datetime_utcnow_bad.py` | consumer instance: `l9.eie.no-datetime-utcnow` |
| `consumer_httpx_async_context_bad.py` | consumer instance: `l9.eie.httpx-async-context-required` |
| `consumer_singleton_lock_bad.py` | consumer instance: `l9.eie.singleton-requires-lock` |
| `consumer_float_try_bad.py` | consumer instance: `l9.eie.float-requires-try-except` |
| `consumer_no_eval_bad.py` | consumer instance: `l9.eie.no-eval` |
| `consumer_no_sql_fstring_bad.py` | consumer instance: `l9.eie.no-sql-fstring` |
| `consumer_no_pickle_loads_bad.py` | consumer instance: `l9.eie.no-pickle-loads` |
| `consumer_no_structlog_getchild_bad.py` | consumer instance: `l9.eie.no-structlog-getchild` |
| `consumer_good.py` | no consumer finding |

Later heads of this branch replace these with one `make validate` / `make check`
leaf witness each (KB rule schema, C-21 import guard, constitution, workflow
YAML, Gate_SDK tag, ruff T20). Run ids are recorded on the PR.
