# EIE `action=converge` — Odoo Gate Consumer Contract

**Scope:** the EIE (worker) side of the PlasticOS Odoo → Gate → EIE enrichment path.
This is the contract `app/engines/handlers.py::handle_converge` and
`app/services/odoo_gate_converge.py` implement and must keep stable for the Odoo
consumer.

> **Full Odoo-side implementation runbook** (SDK pinning, `plasticos_gate` seam,
> ICP wiring, writeback rules, staging e2e, definition-of-done) lives in the Odoo
> repo, not here:
> `cryptoxdog/IB-Odoo_19` → `docs/handoffs/eie-gate-consumer-handoff.md`.
> EIE owns the worker contract below; Odoo owns how it consumes it.

---

## Topology (authority)

```text
Odoo plasticos_gate → Constellation.Gate  POST /v1/execute
  route by header.action == "converge" → EIE /v1/execute → handle_converge
```

- **EIE owns:** the `converge` payload/response contract and the partner-field
  allowlist enforced at the worker boundary.
- **Odoo owns:** when to call Gate, merge-not-overwrite writeback, local fallback.
- **Never:** Odoo → EIE direct HTTP. EIE is only reachable through the hub
  (ADR-002). EIE does not accept Odoo as a direct `/v1/converge` consumer.

---

## Request — `handle_converge` accepts two shapes

Detection is in `is_odoo_converge_payload()`. `entity_snapshot` (dict) or a bare
string `entity_id` (without `entity`) selects the Odoo path; otherwise the legacy
path runs.

### A. Odoo partner-snapshot converge (PlasticOS enrichment runs)

```jsonc
{
  "entity_id": "res.partner:<id>",      // REQUIRED
  "domain": "plasticos",                // defaulted when blank
  "entity_snapshot": {                  // all keys optional; allowlist below
    "name": "...", "website": "...", "city": "...", "zip": "...",
    "street": "...", "street2": "...", "email": "...", "phone": "...",
    "comment": "...",
    "source_urls": ["https://..."]      // optional crawl seeds
  },
  "odoo": {                             // optional audit/context, echoed only
    "model": "plasticos.enrichment.run", "record_id": 7,
    "correlation_id": "plasticos.enrichment.run:7"
  },
  "profile_id": null,                   // omit when null
  "max_passes": null                    // omit when null; EIE bounds 1..10, default 3
}
```

`build_enrich_request()` maps this to the internal `EnrichRequest`
(`object_type` derived from the `entity_id` prefix, `PARTNER_TARGET_SCHEMA` as the
target schema, `correlation_id`/`entity_id` as the idempotency key).

### B. Legacy internal `EnrichRequest` (`entity`/`object_type`/`objective`)

Preserved for internal callers only. Not emitted by PlasticOS enrichment runs.

---

## Response — `map_converge_response`-shaped

Success (`response.payload`):

```jsonc
{
  "run_id": "eie-<ts>-<hex>",
  "status": "ok",
  "pass_count": 2,
  "final_fields": { "website": "https://...", "city": "Raleigh" },
  "writeback": { "partner_fields": { "website": "https://...", "city": "Raleigh" } },
  "total_tokens": 1234,
  "total_cost_usd": 0.05
}
```

On timeout / non-completed convergence the handler **raises** so the hub returns an
error packet and Odoo falls back to its local pipeline. `handle_converge` never
returns a fake-success payload.

---

## Partner writeback allowlist (worker hard boundary)

`PARTNER_WRITEBACK_FIELD_ALLOWLIST` in `app/services/odoo_gate_converge.py`:

```text
name, website, city, zip, street, street2, email, phone
```

Mirrors `plasticos_gate.services.gate_allowlists.PARTNER_WRITEBACK_FIELD_ALLOWLIST`.
`filter_partner_fields()` drops non-allowlisted keys, empty values, and fields the
snapshot already has (Odoo's merge-not-overwrite would ignore them anyway). Keep
the two allowlists in lockstep; changing this set is a coordinated cross-repo edit.

---

## Timeout / failure behavior

- `DEFAULT_CONVERGE_TIMEOUT_SECONDS = 25.0` — under Odoo's 30 s client default.
- Timeout → `TimeoutError` raised → hub error packet → Odoo local fallback.
- Any convergence exception is logged (`handlers.converge_failed`) and re-raised.
- Zero allowlisted fields on success → empty `final_fields`; Odoo treats an empty
  result as a no-op / fallback rather than a claimed writeback.

---

## Contract references

- Registered handler: `docs/contracts/agents/protocols/packet-envelope.yaml`
  (`action: converge`).
- Adapter + tests: `app/services/odoo_gate_converge.py`,
  `tests/unit/test_odoo_gate_converge.py`.
- Gate_SDK `TransportPacket` schema is owned by `Quantum-L9/Gate_SDK`; contract
  changes require a coordinated SHA bump across Odoo / Gate / EIE.
