# ADR-011: EIE Evidence-to-Odoo Mapping Contract

**Status:** Accepted
**Task:** TASK-029
**Date:** 2026-08-02

## Decision

EIE is the producer of the FeatureEvidence → Odoo allowlist mapping contract:

- `contracts/evidence_odoo_mapping/evidence-odoo-mapping.yaml`
- Native loader/policy: `app.models.evidence_odoo_mapping`

Policy locks:
- merge-not-overwrite
- review mode always (proposals only)
- proposal_only write mode
- human-approved Odoo state takes precedence

## Non-goals

- Activating Odoo writeback (TASK-053 consumer / later activation)
- Direct CRM mutation from EIE
- Ranking or match authority (CEG)

## Acceptance

- Contract validates; auto-write / invalid feature negatives reject
- Allowlisted evidence yields `propose_review`
- Human-approved and newer Odoo field state block overwrite
