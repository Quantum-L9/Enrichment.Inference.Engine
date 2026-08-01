# Contract note — audit CRITICAL source fixes (PR #130)

## Summary

Eliminates audit CRITICAL false positives at the source in
`app/services/crm/salesforce_client.py` (SOQL construction) and improves
detection helpers in `tools/audit_engine.py`. No transport or schema contract
changed.

## Verification

- Salesforce SOQL is built from validated identifiers + escaped literals
- Audit engine FastAPI allowlist aligned with C-01 / INV-ARCH-03 intent
