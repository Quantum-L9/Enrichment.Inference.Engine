# Handoff Pack — Wire IB-Odoo_19 as a Gate Consumer (Odoo side)

**Target repo:** `https://github.com/cryptoxdog/IB-Odoo_19`  
**Primary branch (Track A client):** `feat/gate-client-matcher-fallback`  
**Audience:** Agent with write access to IB-Odoo_19 only  
**Related constellation repos (read-only reference for this agent):**
- `https://github.com/Quantum-L9/Gate_SDK` (pip: `constellation-node-sdk`)
- `https://github.com/Quantum-L9/Constellation.Gate` (hub)
- `https://github.com/Quantum-L9/Enrichment.Inference.Engine` (EIE worker)
- `https://github.com/Quantum-L9/Cognitive.Engine.Graphs` (CEG worker — match path)

**Companion EIE work:** PR https://github.com/Quantum-L9/Enrichment.Inference.Engine/pull/128 (`cursor/odoo-converge-handler-66d5`) — EIE `action=converge` handler over SDK `/v1/execute`.

---

## 0. Authority model (non-negotiable)

```text
Gate_SDK + Constellation.Gate + EIE/CEG  =  own transport/runtime rules
Odoo (plasticos_gate)                    =  consumer that must wire correctly
```

### What Odoo owns
- CRM domain models (`res.partner`, enrichment runs, provenance, matcher UI).
- When to call Gate (ICP flags).
- How to map Gate results into Odoo records (allowlists, merge-not-overwrite, fallback to local).
- Operator UX and audit fields on Odoo models.

### What Odoo does NOT own
- `TransportPacket` schema (Gate_SDK).
- Hub routing / `/v1/execute` semantics (Constellation.Gate).
- Worker action registry and handler payload contracts (EIE / CEG).
- Direct HTTP APIs on EIE (`/v1/converge`, `/api/v1/enrich*`) as a consumer path.

### Canonical topology

```text
Odoo plasticos_gate
  → constellation_node_sdk.create_transport_packet(...)
  → GateClient.send_to_gate(packet)          # destination_node="gate" ONLY
  → Constellation.Gate  POST /v1/execute
  → route by header.action
        ├─ "match"    → CEG  /v1/execute
        └─ "converge" → EIE  /v1/execute
  ← response TransportPacket (payload + header.packet_id/correlation_id)
  → Odoo applies allowlisted fields / matcher results OR falls back local
```

**Hard rule (ADR-002):** Never Odoo → EIE or Odoo → CEG direct HTTP. Never import EIE/CEG code into Odoo models.

---

## 1. Mission for this agent

Make IB-Odoo_19 a **correct Gate consumer**:

1. Ensure `constellation-node-sdk` (Gate_SDK) is installed and pinned in the Odoo runtime.
2. Ensure `plasticos_gate` is the **sole** SDK import seam and only talks to Constellation.Gate.
3. Ensure enrichment (`action=converge`) and matching (`action=match`) call paths are operational when `plasticos.gate.url` is set.
4. Align Odoo request builders / response mappers with **live worker contracts** (EIE for converge, CEG for match) — Odoo adapts if mismatched.
5. Preserve try-Gate → local-fallback; never hang; never hard-break ERP flows when Gate is down.
6. Do **not** redesign Gate_SDK, Constellation.Gate, or EIE from the Odoo repo.

---

## 2. Current Track A state (already in IB-Odoo_19)

Work from branch `feat/gate-client-matcher-fallback` (or its merge target if already landed). Do not rebuild this from scratch — audit and fix gaps.

### 2.1 Addon: `plasticos_gate`

| File | Role |
|------|------|
| `plasticos_gate/__manifest__.py` | Declares `external_dependencies.python: ["constellation_node_sdk"]` |
| `plasticos_gate/services/gate_client.py` | **Sole** `constellation_node_sdk` import site; `send_action` / `send_converge_action` / `send_match_action` |
| `plasticos_gate/services/gate_config.py` | ICP helpers, `GateClientConfig`, enablement checks |
| `plasticos_gate/services/gate_contracts.py` | Dataclasses: `ConvergeRequest`, `MatchRequest`, etc. |
| `plasticos_gate/services/gate_builders.py` | Allowlisted request builders from Odoo records |
| `plasticos_gate/services/gate_mappers.py` | Response → Odoo shapes; `extract_audit_metadata` from **header** |
| `plasticos_gate/services/gate_allowlists.py` | Partner/writeback/match field allowlists |
| `plasticos_gate/data/gate_icp_seed.xml` | Default ICPs |

### 2.2 SDK pin (already declared)

In `requirements.txt` (Track A branch):

```text
constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@ab9df5f15c1ba433c3f072a1ca01052584682758
```

EIE currently pins the **same SHA** (via `cryptoxdog/Gate_SDK` remote alias of the same commit). Keep SHA lockstep across Odoo / Gate hub / EIE.

### 2.3 Call sites

| Flow | Odoo entry | Gate action | Worker |
|------|------------|-------------|--------|
| Enrichment | `plasticos.enrichment.run` → `_run_gate_converge()` | `converge` (ICP `plasticos.gate.enrichment_action`) | EIE |
| Matching | buyer matcher / intake match | `match` (ICP `plasticos.gate.matching_action`) | CEG |

Enrichment path: try Gate converge → on exception fall back to local crawl/extract/inject (`enrichment_run.action_execute`).

### 2.4 Seeded ICPs (`plasticos_gate/data/gate_icp_seed.xml`)

| Key | Default | Meaning |
|-----|---------|---------|
| `plasticos.gate.url` | `""` (empty) | **Must be set** to hub base URL or Gate stays off |
| `plasticos.gate.local_node` | `odoo` | `source_node` / `reply_to` |
| `plasticos.gate.matching_enabled` | `1` | Match via Gate when URL+SDK ok |
| `plasticos.gate.matching_action` | `match` | Packet `header.action` for matching |
| `plasticos.gate.enrichment_enabled` | `1` | Converge via Gate when URL+SDK ok |
| `plasticos.gate.enrichment_action` | `converge` | Packet `header.action` for enrichment |
| `plasticos.gate.auto_writeback` | `1` | Live partner writeback (merge-not-overwrite) |
| `plasticos.gate.timeout_seconds` | `30` | Client timeout |
| `plasticos.gate.org_id` | `""` | Tenant org; falls back to `env.cr.dbname` |

---

## 3. Execution checklist (do in order)

### Step 1 — Confirm runtime has Gate_SDK

**Do:**
1. Verify `requirements.txt` (or deploy image / Odoo.sh Python deps) installs:
   ```text
   constellation-node-sdk @ git+https://github.com/Quantum-L9/Gate_SDK.git@<LOCKED_SHA>
   ```
2. Prefer SHA `ab9df5f15c1ba433c3f072a1ca01052584682758` until EIE/Gate announce a coordinated bump.
3. From the Odoo Python env:
   ```bash
   python -c "import constellation_node_sdk; from constellation_node_sdk import GateClient, create_transport_packet, GateClientConfig; print('ok', constellation_node_sdk.__file__)"
   ```
4. Confirm `plasticos_gate/__manifest__.py` still lists `external_dependencies: {"python": ["constellation_node_sdk"]}`.

**Do not:**
- Vendor a copy of Gate_SDK into the Odoo tree.
- Import `httpx`/`requests` to call EIE/CEG URLs from Odoo models.
- Add a second SDK import site outside `gate_client.py`.

**Done when:** `import constellation_node_sdk` succeeds in the Odoo runtime used by workers/UI.

---

### Step 2 — Audit sole-seam client (`gate_client.py`)

**Do:** Read and verify `plasticos_gate/services/gate_client.py` still:

1. Is the **only** module importing `constellation_node_sdk`.
2. Builds packets with:
   ```python
   create_transport_packet(
       action=action,                    # "converge" or "match"
       payload=payload,                  # dict
       tenant={
           "actor": tenant,
           "on_behalf_of": tenant,
           "originator": local_node,     # "odoo"
           "org_id": tenant,
           "user_id": str(user.id) if user else None,
       },
       source_node=local_node,           # "odoo"
       destination_node="gate",          # NEVER "enrichment-engine" / EIE host
       reply_to=local_node,
       correlation_id=correlation_id,
       classification="internal",
       compliance_tags=("ERP", "ENRICHMENT") | ("ERP", "MATCHING"),
   )
   ```
3. Sends via `GateClient(config).send_to_gate(packet)` against `plasticos.gate.url`.
4. Returns `{"packet": response_packet, "payload": dict(response_packet.payload)}`.
5. Raises `GateIntegrationError` (or wraps SDK errors) on transport failure so callers can fall back.

**Config must use** (`gate_config.build_gate_client_config`):

```python
GateClientConfig(
    gate_url=<plasticos.gate.url>,
    local_node=<plasticos.gate.local_node or "odoo">,
    timeout_seconds=float(<plasticos.gate.timeout_seconds or 30>),
    allowed_gate_destination="gate",
)
```

**Done when:** No Odoo code path posts to an EIE/CEG base URL; every intelligence call goes through `send_action` → Gate.

---

### Step 3 — Wire ICP + module install (staging)

**Do:**
1. Install/upgrade modules: `plasticos_gate`, `plasticos_enrichment` (and matcher modules that use Gate match).
2. Set:
   ```text
   plasticos.gate.url = https://<constellation-gate-host>
   ```
   (no trailing junk; must be `http://` or `https://`)
3. Optionally set `plasticos.gate.org_id` to a stable tenant id.
4. Leave defaults unless testing review-only:
   - `enrichment_enabled=1`
   - `auto_writeback=1`
   - `timeout_seconds=30`
5. Confirm enablement helpers:
   - `gate_enrichment_enabled(env)` → True only when URL valid **and** SDK importable **and** flag truthy.
   - Same pattern for matching.

**Done when:** With URL set + SDK present, `_should_try_gate_converge()` / matcher Gate path returns True; with URL empty, returns False and local path is used.

---

### Step 4 — Align converge payload with **EIE live contract**

EIE production ingress is **`POST /v1/execute`** with `header.action == "converge"` (SDK runtime). EIE does **not** accept Odoo as a direct `/v1/converge` consumer.

#### 4.1 What EIE currently accepts (as of EIE PR #128)

EIE `handle_converge` accepts either:

**A. Partner-snapshot converge payload (preferred for PlasticOS enrichment runs):**

```jsonc
{
  "entity_id": "res.partner:<id>",          // required
  "domain": "plasticos",                    // defaulted if blank
  "entity_snapshot": {                      // all keys optional
    "name": "...",
    "website": "...",
    "city": "...",
    "zip": "...",
    "street": "...",
    "street2": "...",
    "comment": "...",
    "email": "...",
    "phone": "...",
    "source_urls": ["https://..."]          // optional seed URLs
  },
  "odoo": {                                 // audit/context; optional keys
    "model": "plasticos.enrichment.run",
    "record_id": 7,
    "company_id": 1,
    "user_id": 2,
    "db_name": "...",
    "correlation_id": "plasticos.enrichment.run:7"
  },
  "profile_id": null,                       // omit when null
  "max_passes": null                        // omit when null; EIE defaults/bounds
}
```

**B. Legacy internal EnrichRequest payload** (not for Odoo CRM enrichment):
`entity`, `object_type`, `objective` (+ optional schema). Do **not** send this from PlasticOS enrichment runs.

#### 4.2 What EIE returns in `response.payload` on success

```jsonc
{
  "run_id": "eie-...",
  "status": "ok",
  "pass_count": 2,
  "final_fields": { "website": "https://...", "city": "Raleigh" },
  "writeback": { "partner_fields": { "website": "https://...", "city": "Raleigh" } },
  "total_tokens": 1234,
  "total_cost_usd": 0.05
}
```

On hard failure / timeout, EIE/hub should surface an **error** (exception / error packet). Odoo must catch and fall back local — do not hang.

#### 4.3 Odoo response application rules (already partially implemented)

In `gate_mappers.py` / `enrichment_run.py`:

1. `map_converge_response(payload)` → `ConvergeResponse`.
2. `partner_writeback_from_converge(resp)`:
   - Prefer non-empty `writeback.partner_fields`, else `final_fields`.
   - Keep only allowlist: `name, website, city, zip, street, street2, email, phone`.
   - Drop `null` / `false` / `""`.
3. Audit from **header** via `extract_audit_metadata(response_packet)`:
   - `gate_packet_id` ← `header.packet_id`
   - `gate_correlation_id` ← `header.correlation_id`
4. If `auto_writeback=1`: merge-not-overwrite onto `res.partner` (only blank fields), write provenance rows, `state=injected`, `engine_used=gate`.
5. If `auto_writeback=0`: store proposal, `state=review`, no partner writes.
6. Treat transport/SDK/hub failures as exceptions → local fallback.

#### 4.4 Agent tasks on Odoo for converge alignment

**Do:**
1. Diff `build_converge_request` / `ConvergeRequest.to_dict()` against §4.1A — ensure required `entity_id`, optional snapshot keys only, `destination` remains Gate (client-level).
2. Diff `map_converge_response` / `partner_writeback_from_converge` against §4.2–4.3.
3. Ensure `_run_gate_converge` uses `send_converge_action` (not raw HTTP).
4. Ensure `correlation_id` passed into `send_converge_action` is stable (`plasticos.enrichment.run:<id>`).
5. If EIE returns `status != "ok"`, treat as failure and fall back (do not mark `injected` with empty junk). Today EIE raises on many failures; still defend against non-ok payload.
6. **Do not** point any ICP or code at EIE’s base URL for enrichment.

**Done when:** A staging enrichment run with Gate+EIE live yields `engine_used="gate"` without any Odoo→EIE direct socket.

---

### Step 5 — Align match payload with **CEG live contract** (secondary for this handoff)

Match is separate from converge but uses the same client seam.

**Do:**
1. Keep `action="match"` (or ICP override) via `send_match_action`.
2. Keep builders/mappers in `gate_builders.py` / `gate_mappers.py`.
3. Require `buyer_partner_id` (or accepted alias `buyer_id`) on results before writing matcher rows.
4. Preserve Gate → local matcher fallback.

Do not block converge wiring on CEG completeness if match is already contracted elsewhere — but do not break the match seam while editing shared `gate_client.py`.

---

### Step 6 — Forbidden surfaces (reject if you find them)

Search the Odoo repo and remove/avoid:

| Forbidden | Why |
|-----------|-----|
| HTTP client calls to EIE `/v1/converge`, `/api/v1/enrich`, `/v1/execute` | Violates ADR-002; bypasses hub |
| HTTP client calls to CEG worker URLs | Same |
| `destination_node="enrichment-engine"` from Odoo | Odoo sends to `"gate"` only; hub routes |
| Importing EIE/CEG Python packages into Odoo addons | Wrong coupling |
| New SDK import sites outside `gate_client.py` | Breaks sole-seam rule |
| Unilateral `TransportPacket` field invention | Bump Gate_SDK + re-pin all nodes |

```bash
# Useful searches inside IB-Odoo_19
rg -n "Enrichment\\.Inference|/v1/converge|/api/v1/enrich|destination_node\\s*=\\s*[\"']enrichment" -g'*.py'
rg -n "import constellation_node_sdk|from constellation_node_sdk" -g'*.py'
rg -n "httpx\\.|requests\\.(get|post)" plasticos_gate plasticos_enrichment -g'*.py'
```

Expected: SDK imports only under `plasticos_gate/services/gate_client.py` (plus TYPE_CHECKING in `gate_config.py` is ok).

---

### Step 7 — Staging end-to-end validation (converge)

**Prerequisites (outside this repo, but required for green e2e):**
- Constellation.Gate deployed and reachable.
- EIE registered on hub for `action=converge` (worker `/v1/execute`).
- Same Gate_SDK SHA across Odoo/Gate/EIE.

**Odoo steps:**
1. Set `plasticos.gate.url`.
2. Create `res.partner` with blank `website` and `city`, attach enrichment source URL(s).
3. Create/run `plasticos.enrichment.run` → `action_execute`.
4. Expect:
   - `run.engine_used == "gate"`
   - `run.state == "injected"` (with `auto_writeback=1`)
   - `run.fields_written > 0`
   - `run.gate_packet_id` and `run.gate_correlation_id` set
   - Partner blank fields filled; non-blank fields untouched
   - `plasticos.enrichment.provenance` rows with `target_model="res.partner"`
5. Negative tests:
   - Stop Gate or set bad URL → local fallback (no crash).
   - `auto_writeback=0` → `state=review`, partner unchanged, proposal stored.
   - Existing `city` value must not be overwritten when Gate returns a different city.

Automated coverage already sketched in `tests/test_gate_enrichment_fallback.py` — keep green; extend if you add status!=ok handling.

---

### Step 8 — Docs / Track B framing cleanup inside Odoo (optional but recommended)

Track B docs currently say “Track B must conform to Track A wire format” and “Owner of contract: PlasticOS Odoo”. That overstates Odoo authority for transport.

**Do (docs-only, if you touch docs):**
- Clarify ADR-002 + this handoff: **Gate_SDK/Gate/EIE own transport; Odoo consumes.**
- Keep documenting the *payload shapes Odoo emits/reads* as the Odoo client contract, not as ownership of EIE/Gate internals.
- Point enrichment e2e at Gate hub URL, never EIE direct.

Do not rewrite constellation repos from this agent.

---

## 4. Exact code anchors (IB-Odoo_19)

Start here when navigating:

```text
plasticos_gate/
  __manifest__.py
  data/gate_icp_seed.xml
  services/
    gate_client.py          # ONLY runtime SDK import + send_to_gate
    gate_config.py          # ICP + GateClientConfig
    gate_contracts.py       # ConvergeRequest / MatchRequest dataclasses
    gate_builders.py        # build_converge_request / build_match_request
    gate_mappers.py         # map_converge_response / partner_writeback_from_converge
    gate_allowlists.py      # PARTNER_WRITEBACK_FIELD_ALLOWLIST

plasticos_enrichment/models/enrichment_run.py
  _should_try_gate_converge
  _run_gate_converge
  _apply_converge_writeback
  action_execute            # Gate first, local fallback

tests/test_gate_enrichment_fallback.py
tests/test_gate_match_contract.py
tests/test_gate_matcher_fallback.py

docs/adr/ADR-002-gate-hub-phased-autonomy.md
docs/track_b/00_AGENT_HANDOFF.md          # topology; correct authority framing if editing
docs/track_b/03_enrichment_inference_engine.md
docs/track_b/04_eie_converge_handler_handoff.md  # EIE worker handoff (other repo)

requirements.txt            # constellation-node-sdk pin
```

---

## 5. TransportPacket fields Odoo must honor

Factory: `constellation_node_sdk.create_transport_packet`.

| Field | Odoo value |
|-------|------------|
| `header.action` | `converge` or `match` (from ICP) |
| `header.correlation_id` | Stable `model:id` string; echoed back |
| `address.source_node` | `odoo` (ICP `local_node`) |
| `address.destination_node` | **`gate`** |
| `address.reply_to` | `odoo` |
| `tenant.*` | org/actor/originator as in `gate_client.send_action` |
| `payload` | Action-specific dict from builders |
| Response `header.packet_id` | Store as `gate_packet_id` |
| Response `header.correlation_id` | Store as `gate_correlation_id` |
| Response `payload` | Mapper input only — not a place for audit ids |

---

## 6. Partner writeback allowlist (CRM hard boundary)

Odoo may write **only**:

```text
name, website, city, zip, street, street2, email, phone
```

Rules:
- Ignore unknown keys from Gate/EIE.
- Drop empty values.
- Merge-not-overwrite: never clobber a non-empty partner field.
- Live by default (`auto_writeback=1`); review-only when `0`.

This allowlist is an **Odoo CRM policy**. EIE may return extra keys; Odoo drops them.

---

## 7. Failure / timeout matrix

| Condition | Expected Odoo behavior |
|-----------|------------------------|
| `plasticos.gate.url` empty | Skip Gate; local path |
| SDK not installed | Skip Gate; local path |
| `enrichment_enabled=0` | Skip Gate converge; local path |
| Gate connection error / timeout | Catch; local fallback; log warning |
| Hub unroutable `converge` | Error → local fallback |
| EIE timeout / exception | Error → local fallback |
| Success, `auto_writeback=1`, fields present | `injected` + partner write + provenance |
| Success, `auto_writeback=0` | `review` + proposal stored |
| Success, zero allowlisted fields | Do not fake success metrics; prefer fallback or explicit no-op handling — do not claim `fields_written>0` |

Never block the Odoo HTTP worker indefinitely; honor `plasticos.gate.timeout_seconds`.

---

## 8. Definition of done (Odoo agent)

- [ ] `constellation-node-sdk` installs cleanly at the locked SHA in the Odoo runtime.
- [ ] `plasticos_gate` is the sole SDK seam; `destination_node="gate"` only.
- [ ] No direct Odoo→EIE or Odoo→CEG HTTP.
- [ ] With hub+EIE live and `plasticos.gate.url` set, enrichment run uses Gate converge:
  - `engine_used="gate"`
  - `state="injected"` (auto-writeback on)
  - `fields_written>0` on sample blank website/city
  - provenance rows present
  - `gate_packet_id` / `gate_correlation_id` populated from **header**
- [ ] Gate down → local fallback without UserError unless local also lacks sources.
- [ ] `auto_writeback=0` → review-only path verified.
- [ ] Merge-not-overwrite verified.
- [ ] Unit/integration tests under `tests/test_gate_enrichment_fallback.py` (and match tests) pass.
- [ ] Docs/comments do not instruct pointing Odoo at EIE direct URLs.

---

## 9. Out of scope for this Odoo agent

- Implementing EIE `handle_converge` (EIE repo / PR #128).
- Implementing Constellation.Gate routing registry.
- Changing Gate_SDK `TransportPacket` schema (requires multi-repo pin bump).
- Phase-3 Gate web-lead triage (`process_web_lead`) — ADR-002 Phase 1 keeps web-lead local.
- Redesigning CEG match algorithms.

If EIE/Gate are not deployed yet, Odoo work is still valid: finish consumer correctness + tests with mocks; e2e waits on hub+worker.

---

## 10. Suggested first commands for the agent

```bash
# In IB-Odoo_19
git fetch origin
git checkout feat/gate-client-matcher-fallback   # or updated default branch containing plasticos_gate

# Dependency
rg -n "constellation-node-sdk|Gate_SDK" requirements.txt pyproject.toml

# Sole-seam audit
rg -n "constellation_node_sdk" -g'*.py'

# Forbidden direct calls
rg -n "/v1/converge|/api/v1/enrich|destination_node" -g'*.py' plasticos_gate plasticos_enrichment

# Read the seam end-to-end
less plasticos_gate/services/gate_client.py
less plasticos_gate/services/gate_config.py
less plasticos_enrichment/models/enrichment_run.py

# Tests (project-standard runner)
# e.g. pytest tests/test_gate_enrichment_fallback.py -q
```

---

## 11. Coordination notes for humans / multi-agent

| Team | Must provide |
|------|----------------|
| Gate hub | Reachable `plasticos.gate.url`; route `converge`→EIE, `match`→CEG; preserve correlation; ≤30s timeout errors |
| EIE | Worker registered; `action=converge` on `/v1/execute`; payload/response per §4 |
| Odoo (this agent) | SDK installed; ICP set; builders/mappers aligned; fallback preserved; no direct worker calls |
| Shared | Gate_SDK SHA lockstep |

When contracts change: **bump Gate_SDK → re-pin Odoo + Gate + EIE together**. Do not ship Odoo-only packet inventions.

---

## 12. One-line summary for the agent

**Install/pin Gate_SDK, talk only to Constellation.Gate with `action=converge|match`, adapt Odoo builders/mappers to live EIE/CEG payload contracts, keep local fallback — never call EIE/CEG directly.**
