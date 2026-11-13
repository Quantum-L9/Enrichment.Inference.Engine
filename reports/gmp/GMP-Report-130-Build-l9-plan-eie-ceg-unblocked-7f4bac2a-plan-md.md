# GMP-130

**Task:** Build .l9/plan/eie_ceg_unblocked_7f4bac2a.plan.md
**Tier:** RUNTIME

## TODOs

- T1: app/engines/observed_association_contract.py REPLACE Add MAPPING_VERIFIED_BY_CEG = "ceg_verified_observedfrom" beside MAPPING_UNVERIFIED_MIRROR; correct the module docstring and the two comments that say D-9 has not landed
- T2: app/services/crm/observed_relationship_producer.py REPLACE In the SYNCED branch, label the result MAPPING_VERIFIED_BY_CEG when assertion.observed_association is not None (via an optional mapping_verification override on _resolved); correct the module docstring and the ObservedRelationshipResult comment
- T3: tests/unit/test_observed_relationship_producer.py REPLACE Unit tests: sent + SYNCED is ceg_verified_observedfrom; sent + CEG refusal and sent + NOT_ACKNOWLEDGED stay unverified; not sent + SYNCED stays unverified
- T4: docs/contracts/dependencies/constellation-gate.yaml REPLACE constellation-gate.yaml: ceg_validation now says the row type check shipped in CEG #454 (older deployments do not check); the D-9 invariant describes both labels; Version 2.1.0 and an Updated entry
- T5: domains/plasticos/spec.yaml REPLACE Correct the pending-CEG wording in the plasticos spec comment and the spec_types docstring (CEG #453/#454 merged)
- T5.2: app/engines/spec_types.py REPLACE Correct the pending-CEG wording in the plasticos spec comment and the spec_types docstring (CEG #453/#454 merged)
- T6: tests/integration/test_observed_relationship_producer.py REPLACE Seam test wording: say the unverified label holds because these runs send no association (send off), not because CEG cannot cross-check
- T7: CHANGELOG.md REPLACE CHANGELOG Unreleased entry for the label and the doc corrections
