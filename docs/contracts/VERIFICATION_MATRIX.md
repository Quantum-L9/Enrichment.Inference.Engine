# Contract verification matrix — Enrichment.Inference.Engine

**Purpose:** Track how each tracked contract lines up with the repo. Update this file when contracts or code change materially.

**Last reviewed:** 2026-04-07 (env-contract aligned to Settings)

| Contract / artifact | Role | Primary code / runtime source | Accuracy (2026-04-07) | Follow-up |
|---------------------|------|------------------------------|------------------------|-----------|
| [`api/openapi.yaml`](api/openapi.yaml) | HTTP surface + schemas | `app/main.py`, `app/api/v1/*` | **Good** — 16 operations; `components.responses` fixed (was wrongly under `paths`). | Keep `x-source-file` line numbers approximate or refresh periodically. |
| [`agents/protocols/_index.yaml`](agents/protocols/_index.yaml) | Packet protocol index | `app/api/v1/chassis_endpoint.py` | **Accurate** — `POST /v1/execute`. | — |
| [`agents/protocols/packet-envelope.yaml`](agents/protocols/packet-envelope.yaml) | Envelope fields / semantics | `chassis/envelope.py`, `chassis/router.py` | **Mostly accurate** — matches inflate/deflate shape. | Reconcile any new envelope keys in code first, then YAML. |
| [`agents/tool-schemas/_index.yaml`](agents/tool-schemas/_index.yaml) | MCP tool registry | `app/agents/mcp_server.py` `TOOL_REGISTRY` / `RESOURCE_REGISTRY` | **Accurate** — names and URIs match. | — |
| [`node.constitution.yaml`](node.constitution.yaml) | Node identity + tracked pack | `scripts/verify_node_constitution.py`, `scripts/l9_contract_control.py` | **Accurate** — version 2.3.0 matches app; paths listed exist. | Add OpenAPI version bump to tracked list if you gate on file hash. |
| [`config/env-contract.yaml`](config/env-contract.yaml) | Documented env vars | `app/core/config.py` `Settings` | **Good** — `variables` list matches Settings 1:1; operational checklists + `other_runtime_env` for OTel. | Refresh when `Settings` fields change. |
| [`enforcement/ci-gate.yaml`](enforcement/ci-gate.yaml) | Selective pytest / scripts | `scripts/l9_contract_control.py`, `tests/contracts/**` | **Accurate** — commands exist. | — |
| [`dependencies/_index.yaml`](dependencies/_index.yaml) + files | External capability contracts | `app/core/config.py`, service clients | **Conceptually accurate** — optional providers match settings knobs. | Each sub-YAML is intent, not proof of full integration. |
| [`events/asyncapi.yaml`](events/asyncapi.yaml) | Async messaging contract | `app/services/event_emitter.py` (and related) | **Not audited in 2026-04-07 pass** | Dedicated diff vs emitter schemas. |
| [`data/graph-schema.yaml`](data/graph-schema.yaml) | Graph model | Neo4j sync / graph clients | **Not audited in 2026-04-07 pass** | Compare to sync Cypher / models. |
| [`data/models/*`](data/models/) | JSON schemas | Pydantic / persistence models | **Spot-check only** | Regenerate or link from `app/models/`. |
| [`runtime-attestation.schema.json`](runtime-attestation.schema.json) | Attestation payload | Attestation endpoint + tests | **Partially verified** via `l9-constitution-gate` tier2 tests | Extend matrix when attestation shape changes. |

## Workflow alignment (L9 gates)

| Workflow | What it enforces | Contract touchpoint |
|----------|------------------|---------------------|
| [`l9-constitution-gate.yml`](../../.github/workflows/l9-constitution-gate.yml) | Constitution script + tier2 tests + PR contract-bound file pairing | `node.constitution.yaml`, `docs/contracts/**`, `tests/contracts/**` |
| [`l9-contract-control.yml`](../../.github/workflows/l9-contract-control.yml) | `l9_contract_control.py` verify + `ci-gate.yaml` selective commands | `enforcement/ci-gate.yaml` |
| [`compliance.yml`](../../.github/workflows/compliance.yml) | Terminology, FastAPI allowlist, KB YAML | Repo layout (this matrix header matches workflow comments) |

## Definition of “100% accurate”

1. Every **tracked** artifact in `node.constitution.yaml` appears in this matrix with **Accuracy = Good** and a **Primary source** path.  
2. OpenAPI **paths** match mounted routers in `app/main.py` (and documented unmounted routers called out).  
3. **env-contract** either matches `Settings` or explicitly states it is a **superset / ops checklist**.  
4. No invalid OpenAPI: `#/components/...` refs resolve (e.g. `components.responses`).

When you fix a row, bump **Last reviewed** and narrow the **Follow-up** cell.

## Shared CI unblock (2026-08-01)

Re-stamped `tools/l9_enrichment_manifest.yaml` hashes and root governance symlinks after shared Semgrep/format/loader fixes on protected contract files.
