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

Units are stated in each quantity's `description`: the shared declaration syntax
(`app/engines/spec_types.py`) has no unit key, and prose is not part of any
typed shape. `equipment_type_ids` and `feedstock_type` use `type: enum`;
`application_class` is an open string.

Tests: `tests/contracts/test_plasticos_0_6_0_candidate.py`.

## Activation (repository owner only)

Review these exact bytes (sha256
`d0805f5430782a6cf6a4c451446abf324a91a1761cb0e11569239fd5cd082793`), then from the
repository root, with your own reviewer identity and approval evidence:

```bash
.venv/bin/python -m scripts.domain_spec_operator publish --root domains --tenant plasticos \
  --spec domains/plasticos/candidates/0.6.0/spec.yaml \
  --reviewed-input-sha256 d0805f5430782a6cf6a4c451446abf324a91a1761cb0e11569239fd5cd082793 \
  --reviewed-by "<reviewer>" --approval-ref "<approval evidence>" \
  --expected-previous-sha256 7e5787b059835a82f63b24592694b8eac289432133e2d07dcd9b50cfb91d5706 \
&& cp domains/plasticos/candidates/0.6.0/spec.yaml domains/plasticos/spec.yaml \
&& git rm -r domains/plasticos/candidates/0.6.0
```

Then commit `domains/plasticos/`. `tests/contracts/test_plasticos_publication.py`
requires the authoring copy to equal the published revision, which the `cp`
step restores.
