# A8 fixtures — exact `sync` packets EIE emits for PlasticOS

Campaign EIE-NATIVE-CLOSURE-001, lane L3, for the peer campaign's live graph-effect
proof (A8). Generated at EIE `aabf855` by `project_graph_sync_row` +
`_sync_idempotency_key` (`app/engines/packet_router.py:309`, `:370`) from the published
PlasticOS revision `dd09cc9e…` (0.6.2); not hand-written. Gate action `sync`, owner CEG.

Each case gives the packet payload and idempotency key. Expected graph effect is what
A8 must read back from the tenant graph independently (EIE cannot observe it).

| case | expected independent graph read (A8) |
|---|---|
| G01 | one `Facility{facility_id:F-A8-1}`; one `PROCESSES` to `PolymerFamily{code:HDPE}` with `line_id 411`, `direction in` |
| G02 | one `PolymerFamily{code:HDPE}`; two `PROCESSES` edges, `line_id` 411 and 412 |
| G03 | `HDPE` edge `direction in`, `PP` edge `direction out` (no inversion) |
| G04 | child carries no `line_id`; A8 records whether CEG creates an edge without its `edgeidentity` (EIE-side finding, see `e1_census.md`) |
| G11 | any of the above, read through an independent tenant graph query |
| G12 | replay G01 with the same key: no extra Facility, no duplicate `line_id 411` edge |
| G13 | send G01's batch under a tenant other than `plasticos`: EIE sends nothing (proven in EIE: `test_g13_foreign_tenant_resolves_no_contract_and_sends_nothing`); A8 confirms no write in the plasticos graph |
| G15 | after CEG's GDS job sets `community_id`, send G15: `community_id` unchanged (EIE never sends it) |
| G16 | compare the G01 row against CEG's pack; any mismatch is reported by A1/A8; EIE does not alter its spec |
| G17 | CEG fails after EIE persisted: A2 owns the retry; EIE leaves the key pending (`tests/unit/test_converge_graph_sync.py::test_sync_failure_leaves_answer_completed_and_key_pending`) |

CEG acknowledgment accepted as confirmation, and nothing else:
`{"status": "success", "entity_type": "facilities", "synced_count": <len(batch)>}`
(`graph_sync_acknowledged`, `app/engines/packet_router.py:259`).

```json
{
 "G01": {
  "tenant": "plasticos",
  "action": "sync",
  "payload": {
   "entity_type": "facilities",
   "batch": [
    {
     "facility_id": "F-A8-1",
     "name": "A8 Facility 1",
     "processed_polymers": [
      {
       "code": "HDPE",
       "line_id": "411",
       "direction": "in"
      }
     ]
    }
   ]
  },
  "idempotency_key": "eie:sync:plasticos:F-A8-1:fc5e9aa842bbbf35"
 },
 "G02": {
  "tenant": "plasticos",
  "action": "sync",
  "payload": {
   "entity_type": "facilities",
   "batch": [
    {
     "facility_id": "F-A8-2",
     "name": "A8 Facility 2",
     "processed_polymers": [
      {
       "code": "HDPE",
       "line_id": "411",
       "direction": "in"
      },
      {
       "code": "HDPE",
       "line_id": "412",
       "direction": "in"
      }
     ]
    }
   ]
  },
  "idempotency_key": "eie:sync:plasticos:F-A8-2:6efbf430f69f1f8a"
 },
 "G03": {
  "tenant": "plasticos",
  "action": "sync",
  "payload": {
   "entity_type": "facilities",
   "batch": [
    {
     "facility_id": "F-A8-3",
     "name": "A8 Facility 3",
     "processed_polymers": [
      {
       "code": "HDPE",
       "line_id": "501",
       "direction": "in"
      },
      {
       "code": "PP",
       "line_id": "502",
       "direction": "out"
      }
     ]
    }
   ]
  },
  "idempotency_key": "eie:sync:plasticos:F-A8-3:404466de819bc0f0"
 },
 "G04": {
  "tenant": "plasticos",
  "action": "sync",
  "payload": {
   "entity_type": "facilities",
   "batch": [
    {
     "facility_id": "F-A8-4",
     "name": "A8 Facility 4",
     "processed_polymers": [
      {
       "code": "HDPE"
      }
     ]
    }
   ]
  },
  "idempotency_key": "eie:sync:plasticos:F-A8-4:ed4e51d1adb01f01"
 },
 "G15": {
  "tenant": "plasticos",
  "action": "sync",
  "payload": {
   "entity_type": "facilities",
   "batch": [
    {
     "facility_id": "F-A8-1",
     "name": "A8 Facility 1",
     "tier": "tier_1"
    }
   ]
  },
  "idempotency_key": "eie:sync:plasticos:F-A8-1:a4a23e52690b81f8"
 }
}
```
