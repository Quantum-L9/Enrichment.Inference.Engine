# E1 — effective PlasticOS spec and domain-leak census

Campaign EIE-NATIVE-CLOSURE-001, lane L3. Read-only evidence at EIE `aabf855`
(`origin/main`). Witnesses: `tests/unit/test_eie_native_e5_generic_projection.py`
and `tests/unit/test_eie_native_e5_shape_characterization.py`.

## (a) Effective revision

| item | value |
|---|---|
| pointer | `domains/plasticos/current.json` → `sha256 dd09cc9ec8fea4a8766b0f3650cfab83a0b0a894d0e499f9f75405f21ae48cfe` |
| revision | `domains/plasticos/revisions/dd09cc9e…/spec.yaml`, sha256 equal to the pointer |
| receipt | `revisions/dd09cc9e…/receipt.json`: version `0.6.2`, `reviewed_input_sha256` = pointer, previous `85327dfa…`, approval_ref `EIE#364 CTO DIRECTIVE` |
| sample `domains/plasticos/spec.yaml` | sha256 also `dd09cc9e…` (byte-identical today) |
| other retained revisions | `3f7906bc…`, `590f0fdb…`, `7e5787b0…`, `85327dfa…` — superseded; not selected by any pointer |
| loader | `DomainYamlReader.load` (`app/engines/domain_yaml_reader.py:284`): once `current.json` exists the tenant is published and only the pointed revision is served |
| shadowing | a replaced `spec.yaml` beside the pointer does not change the served revision (`test_s01_s02_published_pointer_is_the_only_accepted_revision`) |

### Field → node → sync row

| field | node | evidenceonly | sync row / childsync | owner (spec line → projection) |
|---|---|---|---|---|
| `facility_id` | Facility | no | row id (`idproperty`) | `domains/plasticos/spec.yaml:34` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `name` | Facility | no | row field | `domains/plasticos/spec.yaml:35` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `min_density` | Facility | no | row field | `domains/plasticos/spec.yaml:36` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `max_density` | Facility | no | row field | `domains/plasticos/spec.yaml:37` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `min_mfi` | Facility | no | row field | `domains/plasticos/spec.yaml:38` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `max_mfi` | Facility | no | row field | `domains/plasticos/spec.yaml:39` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `contamination_tolerance` | Facility | no | row field | `domains/plasticos/spec.yaml:40` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `capacity_tons_month` | Facility | no | row field | `domains/plasticos/spec.yaml:42` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `tier` | Facility | no | row field | `domains/plasticos/spec.yaml:43` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `processed_polymers` | Facility | no | row field + childsync → PolymerFamily(code) via PROCESSES | `domains/plasticos/spec.yaml:49` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `city` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:60` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `state` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:61` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `zip_code` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:62` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `material_grade` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:63` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `certifications` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:64` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `street` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:66` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `street2` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:67` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `website` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:68` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `email` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:69` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `phone` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:70` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `material_forms` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:73` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `equipment_type_ids` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:83` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `form_preference_id` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:90` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `process_type` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:91` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `feedstock_type` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:92` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `application_class` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:93` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `handles_bales` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:95` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `handles_regrind` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:96` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `handles_pellet` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:97` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `handles_flake` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:98` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `handles_rollstock` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:99` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `can_remove_metal` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:100` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `can_reduce_moisture` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:101` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `can_filter_fr` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:102` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `can_screen_fines` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:103` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `iso_certified` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:104` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `medical_grade_capable` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:105` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `accepts_spot` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:106` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `prefers_contract` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:107` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `accepts_filled_materials` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:108` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `avg_truckload_lbs` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:110` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `min_lot_size_lbs` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:111` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `max_lot_size_lbs` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:112` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `moisture_tolerance_pct` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:113` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `pcr_pct_min` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:114` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `pcr_pct_max` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:115` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `freeform_notes` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:117` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |
| `application_notes` | Facility | yes | excluded (evidence only) | `domains/plasticos/spec.yaml:118` → `app/engines/spec_types.py:422` `row_properties` → `app/engines/packet_router.py:309` `project_graph_sync_row` |

Row construction is generic: `resolve_graph_sync_contract` (`app/engines/packet_router.py:165`)
reads `sync.endpoints[]`; `row_properties` (`app/engines/spec_types.py:422`) keeps every
non-`evidenceonly` declaration; `project_graph_sync_row` (`app/engines/packet_router.py:309`)
validates (`spec_types.py:324` `value_defects`) and encodes (`spec_types.py:371` `encode_value`).
No cartridge name is read on that path. On the converge path the fields first pass
`admitted_fields` (`app/engines/handlers.py:172`, nulls and unadmitted entries removed)
and then `polymer_capability.shape_graph_fields` (`app/engines/handlers.py:507-514`) — the
one cartridge-named step between admission and the generic projection.

CEG-owned Facility properties EIE does not declare (snapshot
`tests/fixtures/ceg/plasticos.spec.yaml`, CEG `26206e1b`): `entity_ref`, `lat`, `lon`,
`credit_score`, `food_grade_certified`, `community_id`, `rate_per_ton`, `rate_confidence`,
`last_transaction_date` — equal to the spec's `subjects.types[].ceg_owned`. CEG labels with no
EIE endpoint (not produced): `MaterialIntake`, `MaterialForm`, `SupplyOpportunity`,
`BuyerDemand`; `PolymerFamily` is produced only as a childsync target.

## (b) Domain-leak census (`app/**`)

Classes: **RUNTIME_BRANCH** (behaves differently on a cartridge name, or reads a
source-system field), **DEFAULT_CONFIG** (a domain-named constant, no branch),
**COMMENT_DOC**. Reachability from `app/main.py` / `app/engines/handlers.py` import chains and
`tests/compliance/test_module_reachability.py` `UNREACHABLE_BASELINE`.

### On the live request path

| file:line | evidence | class | note |
|---|---|---|---|
| `app/engines/polymer_capability.py:25` | `PLASTICOS_CARTRIDGE = "plasticos"` | DEFAULT_CONFIG | key of the branch below |
| `app/engines/polymer_capability.py:28-37` | `if domain != PLASTICOS_CARTRIDGE: return fields` | RUNTIME_BRANCH | cartridge-name branch; embargoed (D5), retired by relocation campaign |
| `app/engines/polymer_capability.py:40-100` | reads Odoo `capability_id`, `polymer`, `polymer_id.code`, `direction`, `form`, m2o `[id, name]` | RUNTIME_BRANCH | source-system field names |
| `app/engines/handlers.py:507-514` | `shape_graph_fields(resolve_cartridge_id(...), admitted_fields(result))` | RUNTIME_BRANCH (call site) | only when `converge_graph_sync_enabled` |
| `app/engines/handlers.py:420-428` | `domain_id = ... or request.object_type` | RUNTIME_BRANCH | Odoo-producer convention (object_type carries the domain) |
| `app/services/kb_resolver.py:166-178` | hints `polymer`, `material`, `material_type`, `x_material_type`, `material_profile` | RUNTIME_BRANCH | reads an Odoo `x_` field; embargoed |
| `app/services/kb_resolver.py:90-110, 189-213` | `polymertype`/`materialtype`, `materialgrades`, `recyclingrules`, `_polymer` | RUNTIME_BRANCH | plastics-shaped KB index; embargoed |
| `app/services/kb_resolver.py:35-49` | `material_grades`, `recycling_rules`, `polymers` | DEFAULT_CONFIG | |
| `app/models/schemas.py:97-112` | `CONVERGE_IDENTITY_ORDER = (..., "entity._odoo_entity_id")` | RUNTIME_BRANCH | source-system field fallback |
| `app/models/schemas.py:342`, `app/main.py:535` | `kb_polymers` health field | DEFAULT_CONFIG | domain-named API field, no branch |
| `app/engines/converge_request_v2.py:67-73` | `_PRODUCER_CONTEXT = frozenset({"odoo"})` | RUNTIME_BRANCH | accepted and carried, not interpreted |
| `app/services/crm_field_scanner.py:304` | strips `x_`, `custom_`, `cf_`, `c_`, `__c` | RUNTIME_BRANCH | CRM naming conventions |
| `app/models/feature_evidence.py:198` | `EvidenceSource.CRM: ProvenanceSourceType.ODOO` | RUNTIME_BRANCH | every CRM evidence stamped Odoo |
| `app/models/feature_evidence.py:55`, `app/services/crm/providers.py:20` | `ODOO = "odoo"` | DEFAULT_CONFIG | |
| `app/services/crm/odoo_client.py:26, 321`; `crm/__init__.py:38` | `PROVIDER_KEY = "odoo"`; import-time registration | RUNTIME_BRANCH | provider adapter (expected location for source-system names) |

### Imported, demo-only (`simulate` is not a runtime action, `handlers.py:363`)

| file:line | evidence | class |
|---|---|---|
| `app/services/simulation_bridge.py:204-319` | `_PLASTICS_REFERENCE`, `_PLASTICS_SONAR_SCHEMA`, derived rules | DEFAULT_CONFIG |
| `app/services/simulation_bridge.py:325-378, 400, 441, 550-576, 777-808` | `_infer_material_grade`, `_infer_facility_tier`, HDPE/PP checks, `x_` stripping, unconditional plastics fallback | RUNTIME_BRANCH |
| `app/services/simulation_bridge.py:457, 643, 1205-1211` | default industry `plastics recycling / polymer recovery`; default query profile | DEFAULT_CONFIG |

### Unreachable (per `UNREACHABLE_BASELINE`)

| file:line | evidence | class |
|---|---|---|
| `app/score/score_icp_plastics.py:20-187` | `PLASTICS_ICP`, `primary_resin = HDPE`, `facility_tier` | DEFAULT_CONFIG |
| `app/services/inference_rule_registry.py:222-339` | `material_type` default `HDPE`, `facility_tier` rules | RUNTIME_BRANCH (dead) |
| `app/engines/inference/nary_inference_engine.py:192-220` | `RECYCLES_GRADE_FOR_BUYER` rule defs | DEFAULT_CONFIG (unwired) |
| `app/models/evidence_odoo_mapping.py:7-141` | `odoo_model`, `odoo_field` loader | DEFAULT_CONFIG (no app caller) |
| `app/engines/inference/grade_engine.py:157` | `kb_data.get("material_grades", ...)` | RUNTIME_BRANCH (no app caller) |

### Domain-neutral (checked, no branch)

| file:line | finding |
|---|---|
| `app/core/config.py:144` | `default_domain: str = ""`; plasticos appears only in comments (`:118-119`) |
| `app/engines/spec_types.py:7-12` | `processed_polymers` only in a docstring example |
| `app/engines/domain_spec_schema.py:205` | comment only |
| `app/engines/field_classifier.py:126, 388, 558-602` | `facility_size` pattern (generic), prompt prose, commented YAML |
| `app/engines/packet_router.py` | no cartridge name or source-system field on the row path |

Comment-only Odoo/plastics mentions also appear in `handlers.py`, `schemas.py`,
`converge_contract.py`, `convergence_controller.py`, `schema_discovery.py`,
`inference_bridge_v2.py`, `inference_bridge_adapter.py`, `rule_loader.py`,
`request_deadline.py`, `idempotency.py`, `validation_engine.py`, `consensus_engine.py`,
`graph_return_channel.py`, `prompt_builder.py`, `auth.py`, `agents/`.

## Gaps on the generic path (evidence for the CTO; no product code changed)

| id | observed at `aabf855` | witness |
|---|---|---|
| G04 | a wire child with `code` and no `line_id` rides the row without edge identity; nothing invents one, nothing refuses it (`line_id` is optional in the spec) | `test_g04_child_without_a_line_id_gets_no_invented_identity` |
| dup | two identical childsync elements are both sent; EIE neither deduplicates nor refuses | `test_duplicate_child_lines_are_sent_as_given_not_merged_or_synthesized` |
| null | `project_graph_sync_row` sends a non-required `None` as a clear; only the converge path removes nulls first (`admitted_fields`) | `test_s12_*` |
