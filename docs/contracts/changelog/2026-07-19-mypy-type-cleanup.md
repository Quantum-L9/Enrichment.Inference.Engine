# Contract note — mypy type cleanup (PR #131)

## Summary

Type-annotation and signature alignment across engine/service modules. No transport
packet schema, handler registration, or `/v1/execute` ingress contract changed.

## Touched contract-adjacent surfaces

- `app/models/loop_schemas.py`, `app/score/score_models.py` — annotation-only /
  hash re-stamp in `tools/l9_enrichment_manifest.yaml`
- CRM / enrichment service modules — typing and safe construction only

## Verification

- `tools/l9_enrichment_manifest.yaml` hashes match current T5 contract files
- Lint (Ruff + Mypy) green on PR head prior to remediation cycle
