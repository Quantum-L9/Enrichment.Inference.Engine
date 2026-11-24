# A8 final run receipts (CTO GATE heads)

Evidence of the signed real-process rail run `20261124T191517Z-ed25519`, copied unchanged from the rail's
evidence directory. Release set: EIE e20d5ed5, CEG a3e7f694, Gate 76a66886, Gate_SDK 1.3.4 (`v1` = 5c1cfc20,
code digest 08b787178555 in all three processes); see `release_set.json`. Every process was observed signing
ed25519. Key-leak check: `summary.json` `leak_check: clean`. The receipts are not edited after the run.

## Campaign verdict

| Verdict | Count | Cases |
|---|---|---|
| PASS | 17 | P03, P05, P07, P09, P10, P11, P11-owner-collision, P13, P14, P17, P18, P19, P23, P27, P33, P34, MIX-ALG-01 |
| FAIL | 1 | P22 (owner CEG-W) |
| BLOCKED | 1 | P30 |
| NOT_RUN | 1 | P12 (lock-mismatch half; the signer half PASS) |
| WITNESSED | 2 | F-A1-05 (DEFERRED by the operator), F-A1-11 |

- **P18 and P05** pass on the enriched path in legacy mode. The request omits the child list, and the
  deterministic provider (#418) supplies it. CEG stores it as typed PROCESSES / HANDLES edges under the same
  tenant, with no stringified map.
- **P22 FAIL, owner CEG-W** (`engine/feedback/graph_inference_trigger.py`). Every requirement held except
  one: the identical resync moved the obligation's `evidence_revision`. CEG recorded `same_findings` and sent
  no duplicate result. The probable cause is inferred, not verified: EIE e20d5ed (#421) now sends
  `sync_version`, which changes on every resync. Per the CTO GATE this run records the failure and does not
  remediate it.
- **P30 BLOCKED.** The inference is the driver's ceg-gl rig, as in `P30.json`.
- **F-A1-05 is DEFERRED** by the operator. **F-A1-11** is recorded as the rail witnessed it (`F-A1-11.json`).
