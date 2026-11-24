# A8 integrated run receipts (Gate_SDK 1.3.4)

The evidence files of the signed real-process rail run `20261124T155758Z-ed25519`, copied unchanged from
the rail's evidence directory. Release set: EIE ebf34525, CEG afd6aaac, Gate 7903fa76, Gate_SDK 1.3.4
(`v1` = 5c1cfc20, code digest 08b787178555 in all three processes); see `release_set.ed25519.json`.

- **`.ed25519` is the run's signing profile**, the algorithm every process was observed signing with
  (`observed_signing_algorithm`). It is not a signature over the file. These receipts carry no content
  signature; their integrity is the commit that adds them. A packet-level field such as
  `gate_signature_valid` records a transport check made during the run, not a property of the receipt.
- **Receipts are not edited after the run.** A rail defect found later is fixed in the rail
  (`tests/e2e/gate_loop/rail.py`) and shows in the next run's receipts, not here.
- **MIX-ALG-01's PASS records a 502 by design.** The case proves the worker side: the hmac-sha256 worker
  verified Gate's ed25519 dispatch and answered, and Gate then refused the answer at its own response check
  (`GateDispatchSecurityError`, "worker response failed transport validation"), which is reachable only after
  the worker accepted the dispatch. A worker refusal would be HTTP 401/403 and a FAIL
  (`tests/e2e/gate_loop/README.md`). This receipt's `reason` is empty; the rail now writes that explanation
  into the reason for future runs.
- The run's key-leak check is `summary.ed25519.json` `leak_check: clean`.
