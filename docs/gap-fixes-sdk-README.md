# gap_fixes_sdk — SDK-Adapted Gap Fixes

**Created:** 2026-04-07
**SDK:** `constellation-node-sdk` (Gate_SDK)

This folder contains gap fixes adapted to use the Gate_SDK `TransportPacket` and related types
instead of the custom `PacketEnvelope` / `ContractViolationError` from the original `gap-fixes/`.

## SDK Dependency

```bash
pip install constellation-node-sdk
# or add to pyproject.toml:
# dependencies = ["constellation-node-sdk>=1.0.0"]
```

## Key Differences from Original gap-fixes/

| Original | SDK-Adapted |
|----------|-------------|
| `ContractViolationError` | `TransportIntegrityError`, `TransportValidationError` |
| `PacketEnvelope` | `TransportPacket` |
| `content_hash`, `envelope_hash` | `payload_hash`, `transport_hash` (auto-computed) |
| Manual hash computation | SDK `compute_payload_hash()`, `compute_transport_hash()` |
| `build_graph_sync_packet()` | `create_transport_packet()` + `derive()` |
| `GraphSyncClient` HTTP calls | `GateClient.send_to_gate()` |
| Manual handler registration | `@register_handler()` decorator |

## Files

| File | Gap | Purpose |
|------|-----|---------|
| `enrich/graph_return_channel.py` | Gap-2 | GRAPH→ENRICH bidirectional return channel |
| `enrich/convergence_controller_patch.py` | Gap-2,4,7,8 | Convergence loop patches |
| `enrich/inference_rule_registry.py` | Gap-3 | Production inference rules |
| `shared/audit_persistence.py` | Gap-5 | PostgreSQL audit persistence |
| `graph/community_export.py` | Gap-6 | Louvain community export to ENRICH |
| `shared/inference_bridge_v1_guard.py` | Gap-9 | v1 bridge import guard |
| `startup_wiring.py` | All | Startup wiring recipe |

## Integration

1. Add `constellation-node-sdk` to dependencies
2. Import from `gap_fixes_sdk/` instead of `gap-fixes/`
3. Call `apply_all_gap_fixes()` at startup
4. Use `GateClient` for all inter-node communication

## SDK Architectural Rules

1. Nodes **MUST NOT** know peer node URLs
2. Nodes **MUST ONLY** send follow-up work to `GATE_URL`
3. Gate is the sole routing authority
4. `TransportPacket` is the only supported transport format
5. Semantic changes create child packets via `derive()`
6. Observational movement appends hop trace entries via `with_hop()`
