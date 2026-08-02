# ADR-012: EIE contract compiler validator

**Status:** Accepted
**Task:** TASK-059
**Date:** 2026-08-02

## Decision

Add `tools/payload_contract_compiler.py` as the EIE contract compiler validator.

It:

- Draft-2020-12 checks FeatureEvidence and evidence-odoo-mapping schemas
- Validates positive/negative fixtures against native models
- Confirms FeatureEvidence + `load_evidence_odoo_mapping` remain the sole evidence authorities
- Emits a deterministic digest report under `artifacts/`

It does **not** create a parallel domain cartridge, alternate transport envelope, or auto-write path into Odoo.

## Consequences

- `make agent-check` runs the compiler validator
- TASK-060 may compare CEG/EIE compiler digests for parity
