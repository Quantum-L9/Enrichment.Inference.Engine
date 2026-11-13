# plasticos 0.6.0 — prepared candidate, NOT approved, NOT active

`spec.yaml` here is the complete candidate for plasticos 0.6.0. It is not served:
production loads only `domains/plasticos/current.json` (still 0.5.0,
`7e5787b0…`) and `domains/plasticos/spec.yaml` stays byte-identical to that
published revision. No receipt or approval exists for these bytes.

## What it adds

The 27 stored data fields of Odoo `plasticos.facility.profile` (IB-Odoo_19) that
0.5.0 did not already declare under another name. Each one is a property of
`Facility`, declared under its Odoo name, `evidenceonly: true` (never on a CEG
sync row), and a field of the `facility` subject. Every 0.5.0 declaration, the
`material` subject, the roles, the node list, `ceg_owned` and `sync` are
unchanged. `food_grade_certified` stays CEG-owned and undeclared.

Operator decisions of 2026-11-13 (scope: all 27 fields; capacity in metric
tonnes; contamination in percent; `food_grade_certified` stays CEG-owned; the
`material` subject means the facility's material capability):

- Every quantity declares its `unit`, served to the consumer as
  `field_evidence[*].declaration.unit` (response.v2.md section 10.2): `lb`,
  `percent`, `g/cm3`, `g/10min`, and `t/month` (metric tonne) for
  `capacity_tons_month`. `unit` is not part of the typed row shape, so the CEG
  sync row is unchanged (`test_ceg_sync_row_is_identical_to_0_5_0`).
- `contamination_tolerance` is a percent (0-100), as CEG declares it. The two
  HDPE grade rules now test 3 and 7 percent instead of the fractions 0.03 and
  0.07, and carry `rule_or_model_version: plasticos-inference-0.6.0`; the
  query-schema description says percent. No other rule changes.
- `equipment_type_ids` and `feedstock_type` use `type: enum`;
  `application_class` is an open string.

Tests: `tests/contracts/test_plasticos_0_6_0_candidate.py`.

## Activation (repository owner only)

Review these exact bytes (sha256
`590f0fdb6ce1181b4a716f3e00512bf5425ba42d266208aa4dab18afca8dafdb`), then from the
repository root, with your own reviewer identity and approval evidence:

```bash
.venv/bin/python -m scripts.domain_spec_operator publish --root domains --tenant plasticos \
  --spec domains/plasticos/candidates/0.6.0/spec.yaml \
  --reviewed-input-sha256 590f0fdb6ce1181b4a716f3e00512bf5425ba42d266208aa4dab18afca8dafdb \
  --reviewed-by "<reviewer>" --approval-ref "<approval evidence>" \
  --expected-previous-sha256 7e5787b059835a82f63b24592694b8eac289432133e2d07dcd9b50cfb91d5706 \
&& cp domains/plasticos/candidates/0.6.0/spec.yaml domains/plasticos/spec.yaml \
&& git rm -r domains/plasticos/candidates/0.6.0
```

Then commit `domains/plasticos/`. `test_plasticos_0_6_0_candidate.py` needs no
edit: it reads these bytes from the candidate copy or, once that is removed, from
the published revision (pinned by digest), and asserts whichever state it finds. `tests/contracts/test_plasticos_publication.py`
requires the authoring copy to equal the published revision, which the `cp`
step restores.
