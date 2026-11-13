# Odoo converge-mapper parity fixtures

EIE-side copy of what the Odoo consumer expects from a converge response, so a
change to the response contract can be checked against the consumer without
reading or writing Odoo.

| | |
|---|---|
| Source repository | `cryptoxdog/IB-Odoo_19` (read-only for EIE) |
| Source commit | `3ad23289bd8fc8a8d10954bca9c2d4517d23a05c` |
| Vendored | `plasticos_gate/services/gate_mappers.py` lines 196-271 (`_sdk_failure_reason`, `map_converge_response`, `partner_writeback_from_converge`) and `plasticos_gate/services/gate_allowlists.py` lines 17-26 (`PARTNER_WRITEBACK_FIELD_ALLOWLIST`) |
| Checked by | `tests/contracts/test_converge_response_v2_odoo_parity.py` |

## Files

| File | Content |
|---|---|
| `odoo_converge_mapper.py` | Transcription of the vendored functions. Returns a plain dict instead of Odoo's `ConvergeResponse` dataclass; the keys are that dataclass's field names. No behaviour is added or removed. |
| `mapper_cases.json` | Input/expected pairs taken from Odoo's own mapper tests (`tests/test_gate_canonical_contract.py`, `tests/test_gate_match_contract.py`). They hold the transcription to the source's behaviour. |
| `expected/*.json` | For each `docs/contracts/converge/examples/response.v2.*.json`: the hand-written result the Odoo mapper must produce (`status`, `final_fields`, `partner_writeback`) and the fields that must never reach Odoo as a value. |

## What this does and does not show

It shows that the response v2 **draft examples**, read by a faithful copy of
the v1 Odoo mapper at the commit above, give the expected v1 result and never
expose an `unknown` or `conflicting` field as a value.

It does not show that a running Odoo persists v2 evidence (it does not read
`field_evidence`), and it does not track Odoo after the commit above. When the
Odoo mapper changes, re-vendor and update the commit in this table.

All data is synthetic. Nothing here comes from an ERP extract.
