# EIE-CRM-001 implementation validation receipt

**Campaign:** EIE CRM Writeback Completion Campaign v1.1.0
**Repository:** `Quantum-L9/Enrichment.Inference.Engine`
**Base revision:** `3f96eda98240789963b0e6534aa2151aed12658e` (`main`)
**Validation environment:** Python 3.12.3; project-local `.venv`; no CRM credentials, provider sandbox records, or production environment used.

## Implemented, source-level scope

- Provider-neutral connector- and provider-account-scoped record references, typed read failures, zero/one/many lookup results, write lifecycle receipts, and a legacy `WriteResult` projection that never treats an indeterminate mutation as successful.
- Odoo, HubSpot, and Salesforce adapter conformance for normalized IDs, remote-read failures, pagination completeness, documented acknowledgement handling, timeout/transport indeterminate outcomes, and R1 refusal of direct create/upsert/bulk paths. HubSpot verifies a configured portal identity on connection.
- Registry-driven, injected-trust `UPDATE_BY_ID` orchestrator. It accepts a trusted connector binding, an approval bound to exact provider-account/operation/record/payload, and a persistence-owner supplied journal protocol; it filters identity, ownership, relationship, and unallowlisted fields before composing a provider request. The internal permit is issuer-bound and digest-bound to the exact outbound field set.
- Explicit no-I/O denial in the currently registered SDK handler and MCP compatibility bridge while trusted tenant connector, approval, and durable journal owners remain unavailable.

## Executed checks

| Check | Result |
|---|---|
| `ruff format --check` and `ruff check` on changed CRM, handler, MCP, and tests | Passed |
| `mypy app/services/crm` | Passed: 7 source files |
| Post-review CRM / SDK / MCP / transport / registration focused test selection | Passed: 141 tests |
| Full repository `pytest -q` after final review fixes | Passed: 2,172; skipped: 8; xfailed: 4 |
| Falsifying mutation: temporarily change Odoo's `acknowledged is True` branch to `False`, run `test_odoo_true_write_acknowledgement_is_confirmed`, then restore and rerun final validation | Expected failure observed (1 failed); final full suite confirms the corrected source |
| `PATH="$PWD/.venv/bin:$PATH" make agent-check` | Lint and format passed; stopped at 29 pre-existing global MyPy errors in 16 unrelated files. Changed CRM package MyPy check passed. |

The first `make agent-check` invocation also demonstrated that the repository assumes an activated virtual environment: its `ruff` executable was not on the shell `PATH`. The rerun above used the project-local environment without modifying repository tooling.

An independent review was performed before final validation. It found the deliberately temporary acknowledgement mutation, plus real concerns around payload-bound permits, provider-account binding, relationship targets, and post-dispatch finalization. The final source binds permits to exact payload digest, includes provider account in approval/journal identity, denies relationship targets even when allowlisted, and records adapter exceptions after durable dispatch as `INDETERMINATE` with reconciliation required.

## Evidence tier and activation disposition

This receipt establishes **SOURCE_PRESENT** evidence only. It is not provider-sandbox, production, Gate-route, approval, persistence, or deployment evidence. The safe runtime disposition is **DISABLED**; no external CRM write path was activated or exercised.

Live activation remains blocked pending all of the following: an authoritative tenant-to-connector/secret/field-allowlist owner; an actor-, record-, and exact-payload-bound approval authority; a reviewed durable CRM operation journal with atomic multi-worker claim/recovery semantics; the separately owned Gate signed caller/action/tenant policy and capability route; real HubSpot sandbox update-by-ID acknowledgement plus independent readback/audit receipt; and required protected-file reviews. The companion [U00 evidence record](EIE-CRM-001-U00-evidence.json) enumerates these boundaries and consumer dispositions.

## Amendment 2026-11-11: provider-agnostic writeback layer

The orchestration layer above the adapters no longer names a provider. This
amendment records what changed and what it does not change.

- **Activation gate.** `WriteBackOrchestrator.write_update_by_id` refuses only on
  `CRMActivationLevel.DISABLED`. The former `provider_not_enabled_for_r1` guard,
  which hard-coded HubSpot as the one enabled provider, is gone; which
  connectors may write is the connector owner's per-connector decision.
- **Open provider registry.** `app/services/crm/base.py` owns
  `CLIENT_REGISTRY`, keyed by a normalized string (`crm_provider_key`), with
  `register_crm_client` / `resolve_crm_client`. Each shipped adapter registers
  itself at import; the package `__init__` imports them. `CRMType` survives in
  `app/services/crm/providers.py` as a convenience enum for the shipped keys and
  is consulted by nothing on the write path. `CRMCredentials.crm_type` and
  `CRMRecordRef.crm_type` are plain strings; digests and permit checks compare
  normalized keys.
- **One mapping shape.** Every `config/crm/*.yaml` declares its object under
  `object`; the per-provider keys (`odoo_model`, `salesforce_object`,
  `hubspot_object`) are rejected with an error naming the domain. Provider
  identity/ownership exclusions live in each file's `protected_fields`; the
  mapper keeps only the generic `*_id` / owner / parent / `id` rules.
- **No per-provider settings.** `Settings.odoo_*`, `salesforce_*`,
  `hubspot_access_token` and `crm_mapping_path` are removed, with their
  `env-contract.yaml` and `.env.example` entries. The `TrustedCRMConnector` is
  the sole owner of credentials, mapping path and activation.
- **Legacy mapper surface removed.** `FieldMapper.to_crm` and
  `get_extra_domain` (Odoo-style lookup clauses) are gone; nothing on the write
  path called them.
- **Proof.** `tests/test_crm_writeback_orchestrator.py` registers a fourth,
  test-only provider through the public registry and drives an update-by-ID
  through the orchestrator with no edit outside that adapter and its mapping;
  it also proves the legacy object key is rejected and that a mapping file's
  `protected_fields` are honoured.

Unchanged: the SDK handler `handle_writeback` still refuses every packet, the
MCP bridge still refuses before dispatch, adapters still refuse create / upsert
/ bulk, permits stay digest-bound, and the runtime disposition is still
**DISABLED**. Adding a CRM is now one adapter module that calls
`register_crm_client` plus one mapping file.
