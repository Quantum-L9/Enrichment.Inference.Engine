# FeatureEvidence compile notes (TASK-041)

Compiles live EIE `FieldConfidence` / `FieldConfidenceMap` into the reviewed
`FeatureEvidence` payload shape.

## Alignment rules

- Canonical dotted `feature_id` (registry-backed when known).
- `confidence` remains 0..1 from FieldConfidence.
- Inferred evidence requires `provenance.rule_or_model_version`
  (explicit argument or first `kb_fragment_ids` entry).
- Transport envelope fields remain forbidden on this payload.
- Convergence response path is preserved; this is an additive compiler.

## Pack draft reference

Pack draft schema URN: `urn:l9:plasticos:contracts:payload:feature-evidence:1.0.0-draft`
(local schema `$id` matches).

## Odoo fixture

See `fixtures/odoo_projection.yaml` for FeatureEvidence → Odoo projection
mapping (proposal_only; no writeback activation in this task).
