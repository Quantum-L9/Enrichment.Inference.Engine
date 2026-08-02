# EIE Evidence → Odoo mapping contract (TASK-029)

Producer-side allowlist for FeatureEvidence fields that may become Odoo
review proposals. Consumers live in IB-Odoo_19 (`plasticos_gate`, TASK-053).

- Schema: `evidence-odoo-mapping.schema.yaml`
- Contract: `evidence-odoo-mapping.yaml`
- Native loader: `app.models.evidence_odoo_mapping`

Policy locks:
- `merge_strategy: merge_not_overwrite`
- `review_mode: always`
- `default_write_mode: proposal_only`
- `human_review_precedence: true`
