#!/usr/bin/env python3
"""gate_loop real-process rail orchestrator (TEST_ONLY), lane PROOF.

The orchestrator runs on the host. Every packet goes through Gate, sent by the
driver container (scripts/drive.sh) or by a node's own egress. The host reads
state directly from the stores the nodes own: Neo4j (``cypher-shell`` in the
neo4j container) and EIE Postgres (``psql`` in the eie-postgres container).
It never trusts an ack it has not read back. It restarts and stops processes
between steps to drive the fault rail.

    python3 rail.py run [--profile ed25519|hmac] [--cases P07,P14,...]

Every case writes one evidence record. It returns PASS, FAIL, NOT_RUN,
NOT_APPLICABLE or BLOCKED, each with a stated reason. PASS is emitted only
when all three hold:
  * the assertion was observed in a real run;
  * the run used the target signing profile (ed25519);
  * the case's proof level is met.
Under the diagnostic HMAC profile, a case that would pass is recorded as
DIAGNOSTIC_OK instead and is never PASS (verdict protocol: class D).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
EIE_ROOT = HERE.parents[2]
STATE = Path(os.environ.get("GL_STATE_DIR", "/root/l9e2e/gate_loop"))
ENV_FILE = Path(os.environ.get("GL_ENV_FILE", STATE / "run.env"))


# ───────────────────────────── plumbing ─────────────────────────────


def env() -> dict[str, str]:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v.strip("'")
    return out


def sh(
    cmd: list[str], *, check: bool = False, timeout: int = 600, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    e = {**os.environ, **(extra_env or {})}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check, env=e)


def rail(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return sh(["bash", str(HERE / "scripts/rail.sh"), *args], extra_env=extra_env, timeout=1200)


def drive(scenario: str, args: dict | None = None) -> dict:
    r = sh(["bash", str(HERE / "scripts/drive.sh"), scenario, json.dumps(args or {})], timeout=600)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"driver_error": (r.stderr or r.stdout)[-800:]}


def cypher(db: str, query: str) -> list[list[str]]:
    pw = env()["GL_NEO4J_PASSWORD"]
    r = sh(
        [
            "docker",
            "exec",
            "l9e2e-neo4j",
            "cypher-shell",
            "-u",
            "neo4j",
            "-p",
            pw,
            "-d",
            db,
            "--format",
            "plain",
            query,
        ]
    )
    if r.returncode != 0:
        return [["CYPHER_ERROR", r.stderr.strip()[-300:]]]
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return [ln.split(", ") for ln in lines[1:]]  # drop header


def psql(query: str) -> list[str]:
    r = sh(
        ["docker", "exec", "l9e2e-eie-pg", "psql", "-U", "enrich", "-d", "enrich", "-Atc", query]
    )
    return (
        r.stdout.strip().splitlines()
        if r.returncode == 0
        else [f"PSQL_ERROR {r.stderr.strip()[-300:]}"]
    )


def logs(container: str, since: str = "2m", grep: tuple[str, ...] = ()) -> list[str]:
    r = sh(["docker", "logs", container, "--since", since])
    lines = (r.stdout + r.stderr).splitlines()
    lines = [ln for ln in lines if "otel" not in ln and "opentelemetry" not in ln]
    if grep:
        lines = [ln for ln in lines if any(g in ln for g in grep)]
    return lines[-40:]


def wait_ready(timeout: int = 420) -> dict:
    t0 = time.time()
    last: dict = {}
    while time.time() - t0 < timeout:
        r = sh(["curl", "-sS", "--noproxy", "*", "http://127.0.0.1:19000/v1/ready"])
        try:
            last = json.loads(r.stdout)
            if last.get("ready"):
                return {"ready": True, "waited_s": round(time.time() - t0, 1)}
        except Exception:
            last = {"raw": r.stdout[:200]}
        time.sleep(5)
    return {
        "ready": False,
        "waited_s": round(time.time() - t0, 1),
        "last": last.get("problems", last),
    }


def registry() -> dict:
    tok = env()["GL_ADMIN_TOKEN"]
    r = sh(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "-H",
            f"X-Admin-Token: {tok}",
            "http://127.0.0.1:19000/v1/registry",
        ]
    )
    try:
        d = json.loads(r.stdout)
        return (
            {k: sorted(v.get("supported_actions", [])) for k, v in d.items()}
            if isinstance(d, dict)
            else {"raw": d}
        )
    except Exception:
        return {"raw": r.stdout[:200]}


# ───────────────────────────── evidence ─────────────────────────────


@dataclass
class Result:
    id: str
    slice: str
    profile: str
    assertion: str
    verdict: str
    reason: str = ""
    observed: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def verdict(ok: bool, profile: str, *, signed_claim: bool) -> str:
    if not ok:
        return "FAIL"
    if signed_claim and profile != "ed25519":
        return "DIAGNOSTIC_OK"
    return "PASS" if profile == "ed25519" else "DIAGNOSTIC_OK"


def converge_payload(
    entity_id: str,
    tenant: str = "plasticos",
    idem: str | None = None,
    extra: dict | None = None,
    schema: dict | None = None,
) -> dict:
    ent = {
        "id": entity_id,
        "facility_id": entity_id,
        "name": f"TEST_ONLY {entity_id}",
        "contamination_tolerance": 5.0,
        "processed_polymers": [
            {"code": "HDPE", "line_id": f"{entity_id}-l1", "direction": "in", "form": "Regrind"}
        ],
    }
    if tenant == "synthneutral":
        ent = {
            "id": entity_id,
            "station_id": entity_id,
            "title": f"TEST_ONLY {entity_id}",
            "max_load": 40.0,
            "handled_kinds": [{"code": "K1", "slot_id": f"{entity_id}-s1", "direction": "in"}],
        }
    ent.update(extra or {})
    key = idem or f"gl:converge:{tenant}:{entity_id}:{uuid.uuid4().hex[:8]}"
    return {
        "action": "converge",
        "tenant": tenant,
        "key_id": "odoo-gl",
        "idempotency_key": key,
        "payload": {
            "entity": ent,
            "entity_id": entity_id,
            "object_type": tenant,
            "domain": tenant,
            "node_label": "Facility" if tenant == "plasticos" else "Station",
            "objective": "TEST_ONLY gate_loop",
            "kb_context": tenant,
            "schema": schema
            or (
                {"name": "string", "capacity_tons_month": "float", "tier": "string"}
                if tenant == "plasticos"
                else {"band": "string"}
            ),
            "max_variations": 2,
            "consensus_threshold": 0.8,
            "idempotency_key": key,
        },
    }


def facility(db: str, fid: str) -> dict:
    rows = cypher(
        db,
        f"MATCH (f:Facility {{facility_id:'{fid}'}}) OPTIONAL MATCH (f)-[r:PROCESSES]->(p:PolymerFamily) "
        "RETURN f.facility_id, f.name, f.contamination_tolerance, p.code, r.line_id, r.direction, r.form, "
        "apoc.meta.cypher.type(f.processed_polymers)",
    )
    return {
        "rows": rows,
        "facility_count": len({r[0] for r in rows if r and r[0] not in ("NULL", "CYPHER_ERROR")}),
    }


# ───────────────────────────── cases ─────────────────────────────


def c_isolation(p: str) -> list[Result]:
    iso = drive("isolation")
    peers = {h: iso.get(h, "") for h in ("enrichment-engine", "graph", "neo4j", "eie-postgres")}
    eie_to_ceg = sh(["docker", "exec", "l9e2e-eie", "getent", "hosts", "graph"]).returncode
    ceg_to_eie = sh(
        ["docker", "exec", "l9e2e-ceg", "getent", "hosts", "enrichment-engine"]
    ).returncode
    ok = (
        all(str(v).startswith("unresolvable") for v in peers.values())
        and eie_to_ceg != 0
        and ceg_to_eie != 0
    )
    return [
        Result(
            "P03",
            "A8",
            p,
            "no direct peer path: workers cannot resolve each other; driver reaches only Gate",
            verdict(ok, p, signed_claim=False),
            observed={
                "driver": iso,
                "eie->graph_getent_rc": eie_to_ceg,
                "ceg->eie_getent_rc": ceg_to_eie,
            },
            reason="topology witness only; source-level absence of raw peer HTTP is EIE-W/CEG-W's P03 share",
        )
    ]


def c_policy(p: str) -> list[Result]:
    m = drive("policy_matrix")
    cells = {(c["key_id"], c["action"]): c for c in m.get("cells", [])}

    def refused(k, a):
        return cells.get((k, a), {}).get("http_status") == 403

    def reached(k, a):
        return cells.get((k, a), {}).get("reached_worker") is True

    ok10 = (
        refused("eie-gl", "score")
        and refused("ceg-gl", "score")
        and refused("ceg-gl", "health-assess")
        and reached("eie-gl", "health-assess")
        and reached("ceg-gl", "graph-inference-result")
        and refused("eie-gl", "graph-inference-result")
    )
    return [
        Result(
            "P10",
            "A7",
            p,
            "worker score refused; only enrichment-engine may originate health-assess; "
            "graph-inference-result only from graph; refusals before execution",
            verdict(ok10, p, signed_claim=True),
            observed={"cells": m.get("cells", m)},
        )
    ]


def c_negatives(p: str) -> list[Result]:
    n = drive("negatives")
    st = {k: v.get("http_status") for k, v in n.items() if isinstance(v, dict)}
    ok11 = (
        st.get("unknown_action") in (403, 404)
        and st.get("wrong_tenant") == 403
        and st.get("unsigned") == 400
        and st.get("unknown_key_rogue") == 400
        and st.get("corrupted_signature") == 400
        and st.get("key_claims_other_node") == 403
        and st.get("hmac_downgrade_with_random_secret") == 400
        and st.get("hmac_downgrade_with_public_key") in (400, 403)
    )
    ok13 = (
        st.get("replay_second_same_packet_id") in (200, 400) and st.get("expired_deadline") == 400
    )
    if st.get("replay_second_same_packet_id") == 200:
        a, b = (
            n["replay_first"].get("payload") or {},
            n["replay_second_same_packet_id"].get("payload") or {},
        )
        ok13 = ok13 and a.get("query_id") == b.get("query_id")
    out = [
        Result(
            "P11",
            "A7",
            p,
            "unknown action, wrong tenant, forgery, unsigned, HMAC downgrade all fail closed",
            verdict(ok11, p, signed_claim=True),
            observed={
                "http_status": st,
                "detail": {k: v.get("body") for k, v in n.items() if isinstance(v, dict)},
            },
            reason=""
            if ok11
            else "hmac_downgrade_with_public_key accepted: SDK verifier honours packet-declared algorithm (BLOCKED owner Gate_SDK)",
        ),
        Result(
            "P13",
            "A7",
            p,
            "replay of a packet id yields no extra effect; expired deadline refused",
            verdict(ok13, p, signed_claim=True),
            observed={"http_status": st},
        ),
    ]
    if not ok11 and st.get("hmac_downgrade_with_public_key") == 200:
        out[0].verdict = "BLOCKED"
    return out


def c_converge_sync(p: str, tenant: str = "plasticos") -> list[Result]:
    """P07 / P18 / P05: consumer converge -> EIE durable -> EIE egress sync via Gate -> CEG -> Neo4j read."""
    eid = f"gl-{tenant[:4]}-{uuid.uuid4().hex[:6]}"
    req = converge_payload(eid, tenant)
    r = drive("execute", req)
    time.sleep(8)
    db = tenant
    if tenant == "plasticos":
        g = facility(db, eid)
        stored = g["facility_count"] == 1
        typed_edges = any(
            row[3] == '"HDPE"' and row[5] == '"in"' for row in g["rows"] if len(row) > 5
        )
        not_stringified = all(row[7] in ("NULL", "null") for row in g["rows"] if len(row) > 7)
    else:
        rows = cypher(
            db,
            f"MATCH (s:Station {{station_id:'{eid}'}}) OPTIONAL MATCH (s)-[h:HANDLES]->(k:Kind) RETURN s.station_id, k.code, h.slot_id",
        )
        g = {"rows": rows}
        stored = any(row and row[0] == f'"{eid}"' for row in rows)
        typed_edges = any(len(row) > 1 and row[1] == '"K1"' for row in rows)
        not_stringified = True
    persisted = psql(f"select count(*) from enrichment_results where entity_id='{eid}'")
    eie_log = logs(
        "l9e2e-eie", "1m", ("graph_sync", "packet_route_error", "graph_leg", "side_effects")
    )
    resp = r.get("response") or {}
    lineage_ok = (
        resp.get("signing_key_id") == "gate-gl"
        and r.get("gate_signature_valid") is True
        and resp.get("generation", 0) >= 2
        and resp.get("causation_id") is not None
    )
    state = (r.get("payload") or {}).get("state")
    obs = {
        "entity_id": eid,
        "converge": {k: r.get(k) for k in ("ok", "elapsed_s", "error", "gate_signature_valid")},
        "response": resp,
        "state": state,
        "eie_persisted_rows": persisted,
        "graph": g,
        "eie_log": eie_log,
    }
    out = []
    if tenant == "plasticos":
        out.append(
            Result(
                "P07",
                "A8",
                p,
                "signed EIE->Gate->CEG sync with Gate-derived child; row read back from Neo4j",
                verdict(stored and lineage_ok, p, signed_claim=True),
                observed=obs,
                reason="" if stored else "EIE egress sync never reached CEG (see eie_log)",
            )
        )
        if not stored and any("signature required but not present" in ln for ln in eie_log):
            out[-1].verdict = "BLOCKED"
            out[
                -1
            ].reason = "EIE egress GateClient carries no ed25519 key from env (F-A1-04, owner Gate_SDK/EIE-W)"
        out.append(
            Result(
                "P18",
                "A8",
                p,
                "typed processed_polymers -> PROCESSES child edges (direction/form on edge), no JSON-string map",
                verdict(stored and typed_edges and not_stringified, p, signed_claim=False),
                observed=obs,
                reason="" if stored else "no row reached CEG",
            )
        )
        if not stored and out[0].verdict == "BLOCKED":
            out[-1].verdict = "BLOCKED"
    else:
        egress_blocked = any("signature required but not present" in ln for ln in eie_log)
        out.append(
            Result(
                "P05",
                "A8",
                p,
                "synthetic neutral tenant converges and syncs typed child edges with no PlasticOS literal",
                verdict(state == "completed" and stored and typed_edges, p, signed_claim=False),
                observed=obs,
                reason="" if stored else "no row reached CEG",
            )
        )
        if not stored and egress_blocked:
            out[-1].verdict = "BLOCKED"
            out[-1].reason = "EIE egress GateClient carries no ed25519 key from env (F-A1-04)"
    return out


def c_ceg_down(p: str) -> list[Result]:
    """P14: EIE completes while CEG is down; result durable; pending intent recoverable (survives EIE restart)."""
    sh(["docker", "stop", "l9e2e-ceg"])
    eid = f"gl-down-{uuid.uuid4().hex[:6]}"
    r = drive("execute", converge_payload(eid))
    persisted = psql(f"select count(*) from enrichment_results where entity_id='{eid}'")
    time.sleep(3)
    eie_log = logs(
        "l9e2e-eie", "1m", ("graph_sync", "packet_route_error", "graph_leg", "side_effects")
    )
    sh(["docker", "restart", "l9e2e-eie"])
    sh(["docker", "start", "l9e2e-ceg"])
    ready = wait_ready()
    time.sleep(15)
    g = facility("plasticos", eid)
    eie_log2 = logs(
        "l9e2e-eie", "2m", ("graph_sync", "graph_leg", "side_effects", "recover", "outbox")
    )
    state = (r.get("payload") or {}).get("state")
    ok = state == "completed" and persisted == ["1"] and g["facility_count"] == 1
    return [
        Result(
            "P14",
            "A8",
            p,
            "EIE completes while CEG down; durable result; pending graph intent recovered after EIE restart",
            verdict(ok, p, signed_claim=False),
            observed={
                "entity_id": eid,
                "state": state,
                "elapsed_s": r.get("elapsed_s"),
                "persisted": persisted,
                "eie_log_down": eie_log,
                "ready_after": ready,
                "graph_after_recovery": g,
                "eie_log_after": eie_log2,
                "durable_intent_tables": psql(
                    "select table_name from information_schema.tables where table_schema='public' and table_name ilike '%pending%' or table_name ilike '%outbox%'"
                ),
            },
            reason=""
            if ok
            else "pending graph intent is in-memory (F-A1-06); not recovered after restart",
        )
    ]


def c_sync_rejects(p: str) -> list[Result]:
    """P17 / P19 via the misassigned EIE key in the driver (Gate policy admits it; this is CEG ingress behaviour)."""
    bad = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "payload": {
                "entity_type": "facilities",
                "batch": [
                    {"facility_id": "gl-p17-a", "name": "ok row"},
                    {"facility_id": "gl-p17-b", "name": "bad row", "min_density": "0.9"},
                ],
            },
        },
    )
    undeclared = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "payload": {"entity_type": "no-such-endpoint", "batch": [{"facility_id": "gl-p17-c"}]},
        },
    )
    partial = cypher(
        "plasticos",
        "MATCH (f:Facility) WHERE f.facility_id IN ['gl-p17-a','gl-p17-b','gl-p17-c'] RETURN f.facility_id",
    )
    ok17 = (
        not partial
        and ((bad.get("payload") or {}).get("status") != "success")
        and ((undeclared.get("payload") or {}).get("status") != "success")
    )
    row = {
        "facility_id": "gl-p19",
        "name": "dup",
        "processed_polymers": [
            {"code": "PP", "line_id": "gl-p19-l1", "direction": "in", "form": "Pellet"}
        ],
    }
    a1 = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "idempotency_key": "gl-p19-1",
            "payload": {"entity_type": "facilities", "batch": [row]},
        },
    )
    a2 = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "idempotency_key": "gl-p19-2",
            "payload": {"entity_type": "facilities", "batch": [row]},
        },
    )
    edges = cypher(
        "plasticos",
        "MATCH (:Facility {facility_id:'gl-p19'})-[r:PROCESSES]->(:PolymerFamily {code:'PP'}) RETURN count(r)",
    )
    ok19 = edges == [["1"]]
    return [
        Result(
            "P17",
            "A8",
            p,
            "CEG refuses wrong type / undeclared endpoint: whole batch, nothing written, accurate refusal",
            verdict(ok17, p, signed_claim=False),
            observed={"wrong_type": bad, "undeclared": undeclared, "graph": partial},
            reason="sent with the eie-gl key from the driver (worker-key misassignment rig); EIE never builds such a row (A1 vectors)",
        ),
        Result(
            "P19",
            "A8",
            p,
            "same relationship evidence twice: no duplicate edge",
            verdict(ok19, p, signed_claim=False),
            observed={"ack1": a1.get("payload"), "ack2": a2.get("payload"), "edge_count": edges},
            reason="node sync ack has no created/replayed counts (F-A1-01); asserted by graph read",
        ),
    ]


def c_match(p: str) -> list[Result]:
    """P33/P34/P35: sync the A1 vector facilities (eie-gl rig), then consumer match through Gate."""
    vec = json.loads((HERE / "vectors/sync_rows.json").read_text())
    rows = [v["row"] for v in vec["vectors"] if v["id"].startswith("PX-POS")]
    s = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "idempotency_key": f"gl-match-seed-{uuid.uuid4().hex[:6]}",
            "payload": {"entity_type": "facilities", "batch": rows},
        },
    )
    res = {}
    for mv in vec["match_vectors"]:
        if mv["tenant"] != "plasticos":
            continue
        q = {"query": mv["query"], "match_direction": mv["match_direction"], "top_n": 10}
        m = drive("execute", {"action": "match", "tenant": "plasticos", "payload": q})
        cands = (m.get("payload") or {}).get("candidates") or []
        ids = [(c.get("candidate") or {}).get("facility_id") for c in cands]
        res[mv["id"]] = {
            "ids": ids,
            "expect": mv["expect_ids"],
            "status": (m.get("payload") or {}).get("status"),
            "error": m.get("error"),
            "first": cands[:1],
        }

    def tp(i: str) -> list:
        return [x for x in res[i]["ids"] if str(x).startswith("tp-fac")]

    ok33 = sorted(tp("PM-POS-01")) == sorted(vec["match_vectors"][0]["expect_ids"])
    ok34 = (
        tp("PM-NEG-01-WRONG-POLYMER") == []
        and tp("PM-NEG-02-WRONG-DIRECTION") == []
        and tp("PM-NEG-03-NO-ACTIVE-SUPPLY") == []
        and "tp-fac-003" not in tp("PM-POS-01")
        and "tp-fac-002" in tp("PM-POS-01")
    )
    return [
        Result(
            "P33",
            "A8",
            p,
            "valid polymer capability in declared direction matches; hard gates before rank",
            verdict(ok33, p, signed_claim=False),
            observed={"seed_ack": s.get("payload"), "match": res},
        ),
        Result(
            "P34",
            "A8",
            p,
            "wrong polymer / wrong direction / no active supply excluded; unknown tolerance eligible; known unacceptable excluded",
            verdict(ok34, p, signed_claim=False),
            observed={"match": res},
        ),
    ]


def c_inference_dup(p: str) -> list[Result]:
    """P23 / P28 at the EIE intake: the same graph-inference-result delivered twice (ceg-gl rig)."""
    out = {
        "entity_id": "gl-p23",
        "field": "tier",
        "value": "tier_1",
        "confidence": 0.9,
        "rule": "gl-rig",
        "provenance": {},
        "rationale": "rig",
    }
    a = drive(
        "execute",
        {
            "action": "graph-inference-result",
            "tenant": "plasticos",
            "key_id": "ceg-gl",
            "idempotency_key": "gl-p23-a",
            "payload": {"inference_outputs": [out]},
        },
    )
    b = drive(
        "execute",
        {
            "action": "graph-inference-result",
            "tenant": "plasticos",
            "key_id": "ceg-gl",
            "idempotency_key": "gl-p23-b",
            "payload": {"inference_outputs": [out]},
        },
    )
    qa, qb = (
        (a.get("payload") or {}).get("targets_queued"),
        (b.get("payload") or {}).get("targets_queued"),
    )
    ok = qa == 1 and qb == 0
    return [
        Result(
            "P23",
            "A8",
            p,
            "unchanged echo of an inference result enqueues nothing new",
            verdict(ok, p, signed_claim=False),
            observed={"first": a.get("payload"), "second": b.get("payload")},
            reason=""
            if ok
            else "EIE intake has no dedup (F-A1-07); a second identical delivery is queued again",
        )
    ]


def c_gate_restart(p: str) -> list[Result]:
    """P09: Gate restart. Registry starts empty; readiness must degrade; recovery by re-registration only."""
    sh(["docker", "restart", "l9e2e-gate"])
    time.sleep(6)
    reg0 = registry()
    r0 = sh(["curl", "-sS", "--noproxy", "*", "http://127.0.0.1:19000/v1/ready"]).stdout[:300]
    m = drive(
        "execute",
        {
            "action": "match",
            "tenant": "plasticos",
            "payload": {
                "query": {"polymer_type": "PET", "contamination_pct": 1.0},
                "match_direction": "supply_opportunity_to_buyer_facility",
                "top_n": 1,
            },
        },
    )
    rec = wait_ready(timeout=420)
    reg1 = registry()
    ok = (
        ('"ready":false' in r0.replace(" ", ""))
        and m.get("ok") is False
        and rec.get("ready") is True
    )
    return [
        Result(
            "P09",
            "A7",
            p,
            "Gate restart: readiness degrades truthfully, no peer fallback, recovers by live re-registration",
            verdict(ok, p, signed_claim=False),
            observed={
                "registry_after_restart": reg0,
                "ready_after_restart": r0,
                "call_during_outage": m,
                "recovery": rec,
                "registry_after_recovery": reg1,
            },
        )
    ]


def c_rogue_signer(p: str) -> list[Result]:
    """P12: a worker response signed by a key Gate does not trust is refused; SDK code digest identical."""
    rail(
        "compose",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "graph",
        extra_env={"GL_ROGUE_CEG": "1"},
    )
    time.sleep(25)
    m = drive(
        "execute",
        {
            "action": "match",
            "tenant": "plasticos",
            "payload": {
                "query": {"polymer_type": "PET", "contamination_pct": 1.0},
                "match_direction": "supply_opportunity_to_buyer_facility",
                "top_n": 1,
            },
        },
    )
    glog = logs(
        "l9e2e-gate", "1m", ("worker_response_untrusted", "GateDispatchSecurityError", "inbound")
    )
    rail("compose", "up", "-d", "--no-deps", "--force-recreate", "graph")
    rec = wait_ready()
    digests = sdk_digests()
    same = len({v.split()[-1] for v in digests.values()}) == 1
    untrusted = m.get("ok") is False and "502" in str(m.get("error"))
    sub = verdict(untrusted and same, p, signed_claim=True)
    # P12 has two required assertions; only one is exercised here, so the case
    # as a whole is NOT_RUN (partial coverage is never PASS).
    return [
        Result(
            "P12",
            "A7",
            p,
            "SDK lock mismatch blocked AND untrusted response signer refused",
            "NOT_RUN",
            observed={
                "unauthorized_signer_subcase": sub,
                "call": m,
                "gate_log": glog,
                "recovery": rec,
                "sdk_code_digest": digests,
            },
            reason=f"signer sub-assertion {sub}; lock-mismatch sub-assertion not exercised: every image installs "
            "the same SDK code (no mismatched image exists)",
        )
    ]


CASES: dict[str, Callable[[str], list[Result]]] = {
    "isolation": c_isolation,
    "policy": c_policy,
    "negatives": c_negatives,
    "converge_plasticos": lambda p: c_converge_sync(p, "plasticos"),
    "converge_synthneutral": lambda p: c_converge_sync(p, "synthneutral"),
    "sync_rejects": c_sync_rejects,
    "match": c_match,
    "inference_dup": c_inference_dup,
    "ceg_down": c_ceg_down,
    "gate_restart": c_gate_restart,
    "rogue_signer": c_rogue_signer,
}


def sdk_digests() -> dict:
    # Code digest = sha256 over (relative path, file sha256) of every .py file in
    # the installed package, sorted. Install metadata (RECORD, direct_url) is
    # excluded, so two installs of identical code produce the same digest.
    probe = (
        "import importlib.metadata as m,hashlib,pathlib,constellation_node_sdk as s;"
        "root=pathlib.Path(s.__file__).parent;"
        "fs=sorted(p for p in root.rglob('*.py'));"
        "h=hashlib.sha256();[h.update((str(p.relative_to(root))+':'+hashlib.sha256(p.read_bytes()).hexdigest()+'\\n').encode()) for p in fs];"
        "print(m.version('constellation-node-sdk'), len(fs), h.hexdigest())"
    )
    out = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(["docker", "exec", c, "python", "-c", probe])
        out[c] = r.stdout.strip() or r.stderr.strip()[-200:]
    return out


def release_set(profile: str) -> dict:
    imgs = {}
    for n in ("gate", "eie", "ceg"):
        r = sh(
            [
                "docker",
                "image",
                "inspect",
                f"l9e2e/{n}:local",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}} {{.Id}}',
            ]
        )
        imgs[n] = r.stdout.strip()
    packs = {}
    for f in sorted(
        (Path(os.environ["GL_CEG_REPO"]) / "tests/fixtures/test_only_packs").glob("*/spec.yaml")
    ):
        packs[f.parent.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    packs["eie:plasticos"] = hashlib.sha256(
        (EIE_ROOT / "domains/plasticos/spec.yaml").read_bytes()
    ).hexdigest()
    packs["eie:synthneutral"] = hashlib.sha256(
        (HERE / "cartridges/synthneutral/spec.yaml").read_bytes()
    ).hexdigest()
    return {
        "profile": profile,
        "images": imgs,
        "sdk_record_sha256": sdk_digests(),
        "packs": packs,
        "provider_class": "deterministic (app/services/deterministic_provider.py, L9_ENVIRONMENT=test)",
        "registry": registry(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument(
        "--profile", default=os.environ.get("GL_SIGNING", "ed25519"), choices=["ed25519", "hmac"]
    )
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--no-boot", action="store_true")
    a = ap.parse_args()
    os.environ["GL_SIGNING"] = a.profile
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ev = STATE / "evidence" / f"{stamp}-{a.profile}"
    ev.mkdir(parents=True, exist_ok=True)
    if not a.no_boot:
        rail("up")
    (ev / "ready.json").write_text(json.dumps(wait_ready(), indent=1))
    rs = release_set(a.profile)
    (ev / "release_set.json").write_text(json.dumps(rs, indent=1))
    results: list[Result] = []
    for name in a.cases.split(","):
        try:
            res = CASES[name](a.profile)
        except Exception as exc:  # a harness failure is NOT_RUN, never PASS
            res = [
                Result(
                    name,
                    "A8",
                    a.profile,
                    "harness",
                    "NOT_RUN",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            ]
        for r in res:
            results.append(r)
            print(f"{r.id:5} {r.verdict:14} {r.reason[:100]}", flush=True)
            (ev / f"{r.id}.json").write_text(json.dumps(asdict(r), indent=1, default=str))
    secrets_ = [
        v
        for k, v in env().items()
        if k.startswith(("GL_PRIV_", "GL_HMAC_"))
        or k in ("GL_ADMIN_TOKEN", "GL_NEO4J_PASSWORD", "GL_PG_PASSWORD")
    ]
    leaks = [
        f.name for f in ev.glob("*.json") for s in secrets_ if len(s) > 8 and s in f.read_text()
    ]
    summary = {
        "stamp": stamp,
        "profile": a.profile,
        "release_set": rs,
        "leak_check": leaks or "clean",
        "results": [{"id": r.id, "verdict": r.verdict, "reason": r.reason} for r in results],
    }
    (ev / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps({"evidence": str(ev), "leak_check": summary["leak_check"]}))
    return 0 if not leaks else 3


if __name__ == "__main__":
    sys.exit(main())
