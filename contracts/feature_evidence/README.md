# FeatureEvidence contract (EIE)

Payload-only evidence items carried inside Gate_SDK TransportPacket.
Canonical model: `app.models.feature_evidence.FeatureEvidence`.
Acceptance (TASK-019): all evidence attributed and versioned.

## Placement

This payload contract lives under top-level `contracts/feature_evidence/` (TASK-019 writable scope). It is intentionally separate from `docs/contracts/` enforcement indices until a later compile/index task promotes it into the docs suite.

## Compile (TASK-041)

Use `compile_field_confidence_map` to emit FeatureEvidence from live FieldConfidence. See COMPILE_NOTES.md and fixtures/odoo_projection.yaml.

## Compiler validator (TASK-059)

Run `tools/payload_contract_compiler.py` (also `make agent-check` step 8/8) to Draft-2020-12 check schemas via `jsonschema` (fails closed if missing), validate fixtures against native models, and emit a digest report. See `docs/adr/ADR-012-eie-contract-compiler-validator.md`.
