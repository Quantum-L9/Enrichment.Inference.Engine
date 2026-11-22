#!/usr/bin/env python3
"""gate_loop real-process rail orchestrator (TEST_ONLY), lane PROOF.

The orchestrator runs on the host. Every packet goes through Gate, sent either
by the driver container (``scripts/drive.sh``) or by a node's own egress. The
host reads state back from the stores the nodes own: Neo4j through
``cypher-shell`` in the neo4j container, and EIE Postgres through ``psql`` in
the eie-postgres container. An ack is never trusted until it has been read
back. Between steps the host restarts and stops processes to drive the fault
rail.

    python3 rail.py run [--profile ed25519|hmac] [--cases isolation,policy,...] [--no-boot]

Every case writes one evidence record, with a verdict of PASS, FAIL, NOT_RUN,
NOT_APPLICABLE or BLOCKED and a stated reason.

A case records PASS only when all of these hold:

* the assertion was observed in this run;
* every probe it depends on actually completed: a probe or query error is
  NOT_RUN, never an empty result;
* the *running* stack signs with ed25519, checked from the containers'
  environment rather than from the ``--profile`` label.

Under the diagnostic HMAC profile, a case that would pass records
DIAGNOSTIC_OK. That is never PASS (verdict protocol, class D).

Evidence is redacted before it is written. A raw subprocess exception is
never recorded, because one can carry command arguments.
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
OBSERVED_PROFILE: dict[str, str] = {}


# ───────────────────────────── plumbing ─────────────────────────────


class ProbeError(RuntimeError):
    """A probe did not complete. The case must record NOT_RUN, never a result."""


def env() -> dict[str, str]:
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v.strip("'")
    return out


def secrets_() -> list[str]:
    try:
        e = env()
    except OSError:
        return []
    keys = [k for k in e if k.startswith(("GL_PRIV_", "GL_HMAC_")) and not k.endswith("_JSON")]
    keys += ["GL_ADMIN_TOKEN", "GL_NEO4J_PASSWORD", "GL_PG_PASSWORD"]
    return [e[k] for k in keys if k in e and len(e[k]) > 8]


def redact(text: str) -> str:
    for s in secrets_():
        text = text.replace(s, "[REDACTED]")
    return text


def sh(
    cmd: list[str], *, timeout: int = 600, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a command. A timeout returns rc 124 and a message that names no argument."""
    e = {**os.environ, **(extra_env or {})}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=e)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd[:1], 124, "", f"{cmd[0]}: timed out after {timeout}s"
        )


def rail(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return sh(["bash", str(HERE / "scripts/rail.sh"), *args], extra_env=extra_env, timeout=1200)


def drive(scenario: str, args: dict | None = None) -> dict:
    r = sh(["bash", str(HERE / "scripts/drive.sh"), scenario, json.dumps(args or {})], timeout=600)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"driver_error": redact((r.stderr or r.stdout)[-600:])}


def cypher(db: str, query: str) -> list[list[str]]:
    """Rows of a Cypher query (header dropped). Raises ProbeError when the query does not complete."""
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
        ],
        timeout=120,
    )
    if r.returncode != 0:
        raise ProbeError(
            f"cypher on {db} failed (rc {r.returncode}): {redact(r.stderr.strip()[-300:])}"
        )
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return [ln.split(", ") for ln in lines[1:]]


def psql(query: str) -> list[str]:
    r = sh(
        ["docker", "exec", "l9e2e-eie-pg", "psql", "-U", "enrich", "-d", "enrich", "-Atc", query],
        timeout=120,
    )
    if r.returncode != 0:
        raise ProbeError(f"psql failed (rc {r.returncode}): {redact(r.stderr.strip()[-300:])}")
    return r.stdout.strip().splitlines()


def logs(container: str, since: str = "2m", grep: tuple[str, ...] = ()) -> list[str]:
    r = sh(["docker", "logs", container, "--since", since], timeout=60)
    lines = (r.stdout + r.stderr).splitlines()
    lines = [ln for ln in lines if "otel" not in ln and "opentelemetry" not in ln]
    if grep:
        lines = [ln for ln in lines if any(g in ln for g in grep)]
    return [redact(ln) for ln in lines[-40:]]


def gate_events(since: str, action: str) -> list[dict]:
    """Gate's structured ingress/completion events for one action since a docker-logs timestamp."""
    out = []
    for ln in logs("l9e2e-gate", since, (f'"action": "{action}"',)):
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if ev.get("event") in ("gate.ingress", "gate.completed", "gate.failure"):
            out.append(
                {
                    k: ev.get(k)
                    for k in (
                        "event",
                        "action",
                        "source_node",
                        "origin_kind",
                        "generation",
                        "hop_count",
                        "request_id",
                        "root_id",
                        "packet_id",
                        "error_type",
                        "error_message",
                    )
                }
            )
    return out


def ready_once(max_s: float) -> dict:
    r = sh(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "--max-time",
            str(max(1, int(max_s))),
            "http://127.0.0.1:19000/v1/ready",
        ],
        timeout=int(max_s) + 5,
    )
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"raw": r.stdout[:200], "rc": r.returncode}


def wait_ready(timeout: int = 420) -> dict:
    """Poll readiness against a monotonic deadline; no single probe outlives the budget."""
    t0 = time.monotonic()
    deadline = t0 + timeout
    last: dict = {}
    while (left := deadline - time.monotonic()) > 0:
        last = ready_once(min(10.0, left))
        if last.get("ready") is True:
            return {"ready": True, "waited_s": round(time.monotonic() - t0, 1)}
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
    return {
        "ready": False,
        "waited_s": round(time.monotonic() - t0, 1),
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
            "--max-time",
            "10",
            "-H",
            f"X-Admin-Token: {tok}",
            "http://127.0.0.1:19000/v1/registry",
        ],
        timeout=20,
    )
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return {"unavailable": True}
    return (
        {k: sorted(v.get("supported_actions", [])) for k, v in d.items()}
        if isinstance(d, dict)
        else {"raw": d}
    )


def stamp() -> str:
    """A docker-logs --since value meaning 'from now on'."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def running_signing() -> dict:
    """The signing algorithm each running container was actually started with."""
    out = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(
            ["docker", "inspect", c, "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            timeout=30,
        )
        alg = next(
            (
                ln.split("=", 1)[1]
                for ln in r.stdout.splitlines()
                if ln.startswith("L9_SIGNING_ALGORITHM=")
            ),
            None,
        )
        out[c] = alg if r.returncode == 0 and alg else "UNAVAILABLE"
    return out


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
    """PASS only on the target profile *as observed in the running containers*."""
    del signed_claim  # every claim needs the observed target profile for PASS
    if not ok:
        return "FAIL"
    if profile != "ed25519" or set(OBSERVED_PROFILE.values()) != {"ed25519"}:
        return "DIAGNOSTIC_OK"
    return "PASS"


def not_run(rid: str, slc: str, p: str, assertion: str, why: str, **obs: object) -> Result:
    return Result(rid, slc, p, assertion, "NOT_RUN", reason=why, observed=dict(obs))


def converge_payload(
    entity_id: str,
    tenant: str = "plasticos",
    idem: str | None = None,
    extra: dict | None = None,
    schema: dict | None = None,
) -> dict:
    ent: dict = {
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
    default_schema = (
        {"name": "string", "capacity_tons_month": "float", "tier": "string"}
        if tenant == "plasticos"
        else {"band": "string"}
    )
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
            "schema": schema or default_schema,
            "max_variations": 2,
            "consensus_threshold": 0.8,
            "idempotency_key": key,
        },
    }


def node_acked(r: dict, tenant: str, entity_type: str, n: int) -> bool:
    """GRAPH_ACKNOWLEDGED for a node endpoint (CTO disposition of F-A1-01).

    The response came from the graph worker for the right tenant, carries
    status success, echoes the entity_type, reports synced_count == len(batch)
    and carries no rejected_count other than 0. created + replayed == requested
    applies to relationship endpoints only.
    """
    resp, pl = r.get("response") or {}, r.get("payload") or {}
    return (
        resp.get("source_node") == "graph"
        and resp.get("tenant") == tenant
        and pl.get("status") == "success"
        and pl.get("entity_type") == entity_type
        and pl.get("synced_count") == n
        and pl.get("rejected_count") in (None, 0)
    )


def ceg_refused(r: dict, error: str) -> bool:
    """CEG received the packet and answered a typed refusal (a Gate-signed `failure` from graph)."""
    resp, pl = r.get("response") or {}, r.get("payload") or {}
    return (
        r.get("ok") is True
        and r.get("gate_signature_valid") is True
        and resp.get("source_node") == "graph"
        and resp.get("packet_type") == "failure"
        and pl.get("error") == error
    )


def facility(db: str, fid: str) -> dict:
    """Facility read-back. valueType() is a Neo4j 5 built-in (no APOC). Raises ProbeError on query failure."""
    rows = cypher(
        db,
        f"MATCH (f:Facility {{facility_id:'{fid}'}}) "
        "OPTIONAL MATCH (f)-[r:PROCESSES]->(p:PolymerFamily) "
        "RETURN f.facility_id, f.name, f.contamination_tolerance, p.code, r.line_id, r.direction, "
        "r.form, valueType(f.processed_polymers)",
    )
    return {"rows": rows, "facility_count": len({r[0] for r in rows if r and r[0] != "NULL"})}


# ───────────────────────────── cases ─────────────────────────────


def c_isolation(p: str) -> list[Result]:
    a = "no direct peer path: workers cannot resolve each other; the driver reaches Gate and only Gate"
    iso = drive("isolation")
    gate_ok = str(iso.get("gate", "")).count(".") == 3
    peers = {
        h: str(iso.get(h, "")) for h in ("enrichment-engine", "graph", "neo4j", "eie-postgres")
    }
    probes = {
        "eie->graph": sh(
            ["docker", "exec", "l9e2e-eie", "getent", "hosts", "graph"], timeout=30
        ).returncode,
        "ceg->eie": sh(
            ["docker", "exec", "l9e2e-ceg", "getent", "hosts", "enrichment-engine"], timeout=30
        ).returncode,
    }
    obs = {"driver": iso, "getent_rc": probes}
    # getent: rc 2 = key not found (a completed negative lookup); anything else is a probe error.
    if not gate_ok or any(rc not in (0, 2) for rc in probes.values()) or "driver_error" in iso:
        return [
            not_run(
                "P03",
                "A8",
                p,
                a,
                "a probe did not complete (Gate unresolved, or getent rc not in {0,2})",
                **obs,
            )
        ]
    ok = all(v.startswith("unresolvable") for v in peers.values()) and all(
        rc == 2 for rc in probes.values()
    )
    return [
        Result(
            "P03",
            "A8",
            p,
            a,
            verdict(ok, p, signed_claim=False),
            observed=obs,
            reason="topology witness only; source-level absence of raw peer HTTP is EIE-W/CEG-W's P03 share",
        )
    ]


def c_policy(p: str) -> list[Result]:
    a = (
        "worker score refused by the kind rule; worker health-assess only from enrichment-engine (operator "
        "consumer allowed by its policy); graph-inference-result only from graph; refusals before execution"
    )
    m = drive("policy_matrix")
    cells = {(c["key_id"], c["action"]): c for c in m.get("cells", [])}
    if len(cells) != 20:
        return [not_run("P10", "A7", p, a, "policy matrix incomplete", matrix=m)]

    def msg(k: str, act: str) -> str:
        return str(((cells[(k, act)].get("body") or {}).get("detail") or {}).get("message", ""))

    def kind_refused(k: str, act: str) -> bool:
        return cells[(k, act)]["http_status"] == 403 and "operator-only" in msg(k, act)

    def policy_refused(k: str, act: str) -> bool:
        return cells[(k, act)]["http_status"] == 403 and "not permitted to invoke" in msg(k, act)

    def reached(k: str, act: str) -> bool:
        return cells[(k, act)].get("reached_worker") is True

    checks = {
        "eie worker score: kind rule": kind_refused("eie-gl", "score"),
        "ceg worker score: kind rule": kind_refused("ceg-gl", "score"),
        "ceg worker health-assess: kind rule": kind_refused("ceg-gl", "health-assess"),
        "eie worker health-assess: reaches graph": reached("eie-gl", "health-assess"),
        "operator consumer health-assess: reaches graph (WORKER_ORIGINATED applies to workers only)": reached(
            "op-gl", "health-assess"
        ),
        "odoo consumer health-assess: refused by its policy": policy_refused(
            "odoo-gl", "health-assess"
        ),
        "ceg worker graph-inference-result: reaches enrichment-engine": reached(
            "ceg-gl", "graph-inference-result"
        )
        and cells[("ceg-gl", "graph-inference-result")].get("response_source")
        == "enrichment-engine",
        "eie worker graph-inference-result: refused": policy_refused(
            "eie-gl", "graph-inference-result"
        ),
    }
    failed = [k for k, v in checks.items() if not v]
    return [
        Result(
            "P10",
            "A7",
            p,
            a,
            verdict(not failed, p, signed_claim=True),
            observed={"checks": checks, "cells": m["cells"]},
            reason="failed: " + "; ".join(failed) if failed else "",
        )
    ]


def c_negatives(p: str) -> list[Result]:
    n = drive("negatives")
    if "driver_error" in n:
        return [
            not_run("P11", "A7", p, "negatives", "driver did not complete", driver=n),
            not_run("P13", "A7", p, "replay/deadline", "driver did not complete", driver=n),
        ]
    st = {k: v.get("http_status") for k, v in n.items() if isinstance(v, dict)}
    expect11 = {
        "unknown_action": (403, 404),
        "wrong_tenant": (403,),
        "unsigned": (400,),
        "unknown_key_rogue": (400,),
        "corrupted_signature": (400,),
        "key_claims_other_node": (403,),
        "consumer_claims_registered_node_name": (403,),
        "hmac_downgrade_with_random_secret": (400,),
        "hmac_downgrade_with_public_key": (400, 403),
    }
    failed11 = [
        f"{k}={st.get(k)} (expected {v})" for k, v in expect11.items() if st.get(k) not in v
    ]
    first, second = n.get("replay_first") or {}, n.get("replay_second_same_packet_id") or {}
    qid1 = (first.get("payload") or {}).get("query_id")
    checks13 = {
        "first execution succeeded with a query_id": st.get("replay_first") == 200 and bool(qid1),
        "same packet id: replay refused or the identical cached answer": st.get(
            "replay_second_same_packet_id"
        )
        == 400
        or (
            st.get("replay_second_same_packet_id") == 200
            and (second.get("payload") or {}).get("query_id") == qid1
        ),
        "expired deadline refused": st.get("expired_deadline") == 400,
    }
    failed13 = [k for k, v in checks13.items() if not v]
    r11 = Result(
        "P11",
        "A7",
        p,
        "unknown action, wrong tenant, forgery, unsigned and HMAC downgrade all fail closed",
        verdict(not failed11, p, signed_claim=True),
        observed={
            "http_status": st,
            "detail": {k: v.get("body") for k, v in n.items() if isinstance(v, dict)},
        },
        reason="failed: " + "; ".join(failed11) if failed11 else "",
    )
    if st.get("hmac_downgrade_with_public_key") == 200:
        others = [f for f in failed11 if not f.startswith("hmac_downgrade_with_public_key")]
        r11.verdict = "BLOCKED"
        r11.reason = (
            "hmac_downgrade_with_public_key accepted (F-A7-03, owner Gate_SDK: the verifier honours the "
            "packet-declared algorithm)" + ("; also failed: " + "; ".join(others) if others else "")
        )
    r13 = Result(
        "P13",
        "A7",
        p,
        "a replayed packet id adds no effect; an expired deadline is refused",
        verdict(not failed13, p, signed_claim=True),
        observed={"http_status": st, "checks": checks13},
        reason=("failed: " + "; ".join(failed13))
        if failed13
        else "effect count: the replay returned the cached answer (same query_id), i.e. no second execution",
    )
    return [r11, r13]


def c_converge_sync(p: str, tenant: str = "plasticos") -> list[Result]:
    """P07 / P18 / P05: consumer converge -> EIE durable -> EIE's own egress sync via Gate -> CEG -> Neo4j."""
    eid = f"gl-{tenant[:4]}-{uuid.uuid4().hex[:6]}"
    since = stamp()
    r = drive("execute", converge_payload(eid, tenant))
    time.sleep(8)
    ids = ("P07", "P18") if tenant == "plasticos" else ("P05",)
    try:
        persisted = psql(f"select count(*) from enrichment_results where entity_id='{eid}'")
        if tenant == "plasticos":
            g = facility(tenant, eid)
            stored = g["facility_count"] == 1
            edge = [row for row in g["rows"] if len(row) > 7 and row[3] == '"HDPE"']
            typed_edges = any(row[5] == '"in"' and row[6] == '"Regrind"' for row in edge)
            not_stringified = bool(g["rows"]) and all(
                row[7] in ("NULL", '"NULL"') for row in g["rows"]
            )
        else:
            rows = cypher(
                tenant,
                f"MATCH (s:Station {{station_id:'{eid}'}}) "
                "OPTIONAL MATCH (s)-[h:HANDLES]->(k:Kind) RETURN s.station_id, k.code, h.slot_id",
            )
            g = {"rows": rows}
            stored = any(row and row[0] == f'"{eid}"' for row in rows)
            typed_edges = any(len(row) > 1 and row[1] == '"K1"' for row in rows)
            not_stringified = True
    except ProbeError as exc:
        return [
            not_run(i, "A8", p, "converge -> sync read-back", str(exc), entity_id=eid) for i in ids
        ]
    sync_events = gate_events(since, "sync")
    ingress = [
        e
        for e in sync_events
        if e["event"] == "gate.ingress" and e["source_node"] == "enrichment-engine"
    ]
    completed = [
        e
        for e in sync_events
        if e["event"] == "gate.completed"
        and any(e["request_id"] == i["request_id"] for i in ingress)
    ]
    eie_log = logs(
        "l9e2e-eie", "1m", ("graph_sync", "packet_route_error", "graph_leg", "side_effects")
    )
    resp = r.get("response") or {}
    converge_lineage = (
        resp.get("signing_key_id") == "gate-gl"
        and r.get("gate_signature_valid") is True
        and resp.get("generation", 0) >= 2
        and resp.get("causation_id") is not None
    )
    sync_via_gate = (
        bool(ingress)
        and bool(completed)
        and all((c.get("generation") or 0) >= 2 for c in completed)
    )
    egress_blocked = any("signature required but not present" in ln for ln in eie_log)
    state = (r.get("payload") or {}).get("state")
    obs = {
        "entity_id": eid,
        "converge": {k: r.get(k) for k in ("ok", "elapsed_s", "error", "gate_signature_valid")},
        "response": resp,
        "state": state,
        "eie_persisted_rows": persisted,
        "graph": g,
        "gate_sync_events": sync_events,
        "eie_log": eie_log,
    }
    out: list[Result] = []
    if tenant == "plasticos":
        ok07 = stored and converge_lineage and sync_via_gate
        out.append(
            Result(
                "P07",
                "A8",
                p,
                "signed EIE->Gate->CEG sync observed at Gate (ingress from "
                "enrichment-engine, completed, child generation >= 2) and its row read back from Neo4j",
                verdict(ok07, p, signed_claim=True),
                observed=obs,
                reason="" if ok07 else "sync not observed through Gate, or row not read back",
            )
        )
        out.append(
            Result(
                "P18",
                "A8",
                p,
                "typed processed_polymers -> PROCESSES edge with direction in and form "
                "Regrind; Facility carries no processed_polymers property (no JSON-string map)",
                verdict(stored and typed_edges and not_stringified, p, signed_claim=False),
                observed=obs,
                reason="" if stored else "no row reached CEG",
            )
        )
    else:
        ok05 = state == "completed" and stored and typed_edges and sync_via_gate
        out.append(
            Result(
                "P05",
                "A8",
                p,
                "synthetic neutral tenant converges and syncs typed child edges via Gate, "
                "with no PlasticOS literal",
                verdict(ok05, p, signed_claim=False),
                observed=obs,
                reason="" if stored else "no row reached CEG",
            )
        )
    if not stored and egress_blocked:
        for res in out:
            res.verdict = "BLOCKED"
            res.reason = (
                "EIE egress GateClient carries no ed25519 key from env (F-A1-04, owner Gate_SDK)"
            )
    return out


def c_ceg_down(p: str) -> list[Result]:
    """P14: EIE completes while CEG is down; durable result; the pending graph intent survives an EIE restart."""
    a = "EIE completes while CEG is down; durable result; pending graph intent recovered after EIE restart"
    eid = f"gl-down-{uuid.uuid4().hex[:6]}"
    stop = sh(["docker", "stop", "l9e2e-ceg"], timeout=120)
    running = sh(
        ["docker", "inspect", "l9e2e-ceg", "--format", "{{.State.Running}}"], timeout=30
    ).stdout.strip()
    r: dict = {}
    try:
        if stop.returncode != 0 or running != "false":
            return [
                not_run(
                    "P14",
                    "A8",
                    p,
                    a,
                    "the CEG outage could not be established",
                    stop_rc=stop.returncode,
                    running=running,
                )
            ]
        r = drive("execute", converge_payload(eid))
        persisted = psql(f"select count(*) from enrichment_results where entity_id='{eid}'")
        absent_before = facility("plasticos", eid)["facility_count"] == 0
        eie_log = logs(
            "l9e2e-eie", "1m", ("graph_sync", "packet_route_error", "graph_leg", "side_effects")
        )
        outbox = psql(
            "select table_name from information_schema.tables where table_schema='public' "
            "and (table_name ilike '%pending%' or table_name ilike '%outbox%')"
        )
        sh(["docker", "restart", "l9e2e-eie"], timeout=180)
    except ProbeError as exc:
        return [not_run("P14", "A8", p, a, str(exc), entity_id=eid)]
    finally:
        start = sh(["docker", "start", "l9e2e-ceg"], timeout=120)
        ready = wait_ready()
        if start.returncode != 0 or not ready.get("ready"):
            raise ProbeError(
                f"CEG did not come back after the outage case (start rc {start.returncode}, {ready})"
            )
    time.sleep(15)
    try:
        g = facility("plasticos", eid)
    except ProbeError as exc:
        return [not_run("P14", "A8", p, a, str(exc), entity_id=eid)]
    st = (r.get("payload") or {}).get("state")
    ok = st == "completed" and persisted == ["1"] and absent_before and g["facility_count"] == 1
    return [
        Result(
            "P14",
            "A8",
            p,
            a,
            verdict(ok, p, signed_claim=False),
            observed={
                "entity_id": eid,
                "state": st,
                "elapsed_s": r.get("elapsed_s"),
                "persisted": persisted,
                "absent_while_down": absent_before,
                "eie_log_down": eie_log,
                "ready_after": ready,
                "graph_after_recovery": g,
                "durable_intent_tables": outbox,
            },
            reason=""
            if ok
            else "pending graph intent is in-memory (F-A1-06, EIE-W): no outbox table, "
            "not recovered after the EIE restart",
        )
    ]


def c_sync_rejects(p: str) -> list[Result]:
    """P17 / P19 through Gate with the misassigned EIE key (rig): CEG ingress and write behaviour."""
    run = uuid.uuid4().hex[:6]
    bad_ids = [f"gl-p17-{run}-a", f"gl-p17-{run}-b", f"gl-p17-{run}-c"]
    bad = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "payload": {
                "entity_type": "facilities",
                "batch": [
                    {"facility_id": bad_ids[0], "name": "ok row"},
                    {"facility_id": bad_ids[1], "name": "bad row", "min_density": "0.9"},
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
            "payload": {"entity_type": "no-such-endpoint", "batch": [{"facility_id": bad_ids[2]}]},
        },
    )
    fid = f"gl-p19-{run}"
    row = {
        "facility_id": fid,
        "name": "dup",
        "processed_polymers": [
            {"code": "PP", "line_id": f"{fid}-l1", "direction": "in", "form": "Pellet"}
        ],
    }
    acks = [
        drive(
            "execute",
            {
                "action": "sync",
                "tenant": "plasticos",
                "key_id": "eie-gl",
                "idempotency_key": f"{fid}-{i}",
                "payload": {"entity_type": "facilities", "batch": [row]},
            },
        )
        for i in (1, 2)
    ]
    try:
        id_list = ",".join(f"'{i}'" for i in bad_ids)
        partial = cypher(
            "plasticos",
            f"MATCH (f:Facility) WHERE f.facility_id IN [{id_list}] RETURN f.facility_id",
        )
        edges = cypher(
            "plasticos",
            f"MATCH (:Facility {{facility_id:'{fid}'}})-[r:PROCESSES]->"
            "(:PolymerFamily {code:'PP'}) RETURN count(r)",
        )
    except ProbeError as exc:
        return [not_run(i, "A8", p, "sync read-back", str(exc)) for i in ("P17", "P19")]
    checks17 = {
        "wrong type: CEG answered SyncRowRejectedError": ceg_refused(bad, "SyncRowRejectedError"),
        "undeclared endpoint: CEG answered ValidationError": ceg_refused(
            undeclared, "ValidationError"
        ),
        "nothing written (Neo4j read-back empty)": partial == [],
    }
    checks19 = {
        "both deliveries GRAPH_ACKNOWLEDGED": all(
            node_acked(x, "plasticos", "facilities", 1) for x in acks
        ),
        "exactly one PROCESSES edge": edges == [["1"]],
    }
    f17 = [k for k, v in checks17.items() if not v]
    f19 = [k for k, v in checks19.items() if not v]
    rig = "sent with the eie-gl key held by the driver (worker-key misassignment rig); EIE never builds such a row"
    return [
        Result(
            "P17",
            "A8",
            p,
            "CEG refuses wrong type / undeclared endpoint: whole batch, nothing written",
            verdict(not f17, p, signed_claim=False),
            observed={
                "checks": checks17,
                "wrong_type": bad,
                "undeclared": undeclared,
                "graph": partial,
            },
            reason=("failed: " + "; ".join(f17)) if f17 else rig,
        ),
        Result(
            "P19",
            "A8",
            p,
            "same relationship evidence twice: both acknowledged, no duplicate edge",
            verdict(not f19, p, signed_claim=False),
            observed={
                "checks": checks19,
                "ack1": acks[0].get("payload"),
                "ack2": acks[1].get("payload"),
                "edge_count": edges,
                "facility_id": fid,
            },
            reason=("failed: " + "; ".join(f19))
            if f19
            else "node GRAPH_ACKNOWLEDGED (F-A1-01 disposition) plus a Neo4j edge count; fresh ids per run",
        ),
    ]


def c_match(p: str) -> list[Result]:
    """P33/P34: seed the A1 fixture facilities through Gate (rig), then consumer match through Gate.

    Assertions are scoped to the tp-fac- fixture set. Rows other cases wrote into the same tenant database can
    also match; they are recorded under off_fixture_candidates and never asserted on.
    """
    vec = json.loads((HERE / "vectors/sync_rows.json").read_text())
    rows = [v["row"] for v in vec["vectors"] if v["id"].startswith("PX-POS")]
    seed = drive(
        "execute",
        {
            "action": "sync",
            "tenant": "plasticos",
            "key_id": "eie-gl",
            "idempotency_key": f"gl-match-seed-{uuid.uuid4().hex[:6]}",
            "payload": {"entity_type": "facilities", "batch": rows},
        },
    )
    res: dict = {}
    errors = []
    for mv in vec["match_vectors"]:
        if mv["tenant"] != "plasticos":
            continue
        q = {"query": mv["query"], "match_direction": mv["match_direction"], "top_n": 10}
        m = drive("execute", {"action": "match", "tenant": "plasticos", "payload": q})
        pl = m.get("payload") or {}
        if m.get("ok") is not True or pl.get("status") != "success":
            errors.append(mv["id"])
        ids = [(c.get("candidate") or {}).get("facility_id") for c in (pl.get("candidates") or [])]
        res[mv["id"]] = {
            "fixture_ids": sorted(i for i in ids if str(i).startswith("tp-fac-")),
            "off_fixture_candidates": sorted(
                str(i) for i in ids if not str(i).startswith("tp-fac-")
            ),
            "expect_fixture_ids": sorted(mv["expect_ids"]),
            "status": pl.get("status"),
            "error": m.get("error"),
            "first": (pl.get("candidates") or [])[:1],
        }
    if errors or not node_acked(seed, "plasticos", "facilities", len(rows)):
        why = f"seed not acknowledged, or match did not succeed for {errors}"
        return [
            not_run(i, "A8", p, "match", why, seed=seed.get("payload"), match=res)
            for i in ("P33", "P34")
        ]
    per = {k: v["fixture_ids"] == v["expect_fixture_ids"] for k, v in res.items()}
    ok33 = per.get("PM-POS-01", False)
    ok34 = all(
        per.get(k, False)
        for k in (
            "PM-NEG-01-WRONG-POLYMER",
            "PM-NEG-02-WRONG-DIRECTION",
            "PM-NEG-03-NO-ACTIVE-SUPPLY",
            "PM-POS-01",
        )
    )
    return [
        Result(
            "P33",
            "A8",
            p,
            "valid polymer capability in the declared direction matches; hard gates before rank",
            verdict(ok33, p, signed_claim=False),
            observed={"seed_ack": seed.get("payload"), "per_vector_match": per, "match": res},
        ),
        Result(
            "P34",
            "A8",
            p,
            "wrong polymer / wrong direction / no active supply excluded; unknown tolerance "
            "eligible; known unacceptable tolerance excluded",
            verdict(ok34, p, signed_claim=False),
            observed={"per_vector_match": per, "match": res},
        ),
    ]


def c_inference_dup(p: str) -> list[Result]:
    """P23 at the EIE intake: the same graph-inference-result delivered twice (ceg-gl rig)."""
    eid = f"gl-p23-{uuid.uuid4().hex[:6]}"
    out = {
        "entity_id": eid,
        "field": "tier",
        "value": "tier_1",
        "confidence": 0.9,
        "rule": "gl-rig",
        "provenance": {},
        "rationale": "rig",
    }
    a, b = (
        drive(
            "execute",
            {
                "action": "graph-inference-result",
                "tenant": "plasticos",
                "key_id": "ceg-gl",
                "idempotency_key": f"{eid}-{s}",
                "payload": {"inference_outputs": [out]},
            },
        )
        for s in ("a", "b")
    )
    qa, qb = (
        (a.get("payload") or {}).get("targets_queued"),
        (b.get("payload") or {}).get("targets_queued"),
    )
    if qa is None or qb is None:
        return [
            not_run("P23", "A8", p, "echo dedup", "a delivery did not complete", first=a, second=b)
        ]
    ok = qa == 1 and qb == 0
    return [
        Result(
            "P23",
            "A8",
            p,
            "an unchanged echo of an inference result enqueues nothing new",
            verdict(ok, p, signed_claim=False),
            observed={"first": a.get("payload"), "second": b.get("payload")},
            reason=""
            if ok
            else "EIE intake has no dedup (F-A1-07, EIE-W): a second identical delivery is "
            "queued again",
        )
    ]


MATCH_PROBE = {
    "action": "match",
    "tenant": "plasticos",
    "payload": {
        "query": {"polymer_type": "PET", "contamination_pct": 1.0},
        "match_direction": "supply_opportunity_to_buyer_facility",
        "top_n": 1,
    },
}


def c_gate_restart(p: str) -> list[Result]:
    """P09: Gate restart. Readiness must degrade; a call during the outage fails closed; recovery by re-registration."""
    a = "Gate restart: readiness degrades truthfully, no peer fallback, recovery by live re-registration"
    sh(["docker", "restart", "l9e2e-gate"], timeout=120)
    time.sleep(6)
    reg0, r0 = registry(), ready_once(10)
    m = drive("execute", MATCH_PROBE)
    reg_after_call = registry()
    rec = wait_ready(timeout=420)
    obs = {
        "registry_after_restart": reg0,
        "ready_after_restart": r0,
        "call_during_outage": m,
        "registry_after_call": reg_after_call,
        "recovery": rec,
        "registry_after_recovery": registry(),
    }
    if reg0.get("unavailable") or "ready" not in r0:
        return [not_run("P09", "A7", p, a, "Gate did not answer after the restart", **obs)]
    if m.get("ok") is True and reg_after_call and not reg_after_call.get("unavailable"):
        return [
            not_run(
                "P09",
                "A7",
                p,
                a,
                "a re-registration landed before the outage call (race): inconclusive",
                **obs,
            )
        ]
    ok = r0.get("ready") is False and m.get("ok") is False and rec.get("ready") is True
    return [Result("P09", "A7", p, a, verdict(ok, p, signed_claim=False), observed=obs)]


def c_rogue_signer(p: str) -> list[Result]:
    """P12: a worker response signed by a key Gate does not trust is refused; SDK code digest identical."""
    a = "SDK lock mismatch blocked AND untrusted response signer refused"
    try:
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
        m = drive("execute", MATCH_PROBE)
        glog = logs(
            "l9e2e-gate",
            "1m",
            ("worker_response_untrusted", "GateDispatchSecurityError", "inbound"),
        )
    finally:
        rail("compose", "up", "-d", "--no-deps", "--force-recreate", "graph")
        rec = wait_ready()
        if not rec.get("ready"):
            raise ProbeError(f"trusted graph worker not ready after the rogue-signer case: {rec}")
    digests = sdk_digests()
    same = len({v.split()[-1] for v in digests.values()}) == 1
    untrusted = m.get("ok") is False and "502" in str(m.get("error"))
    sub = verdict(untrusted and same, p, signed_claim=True)
    # Two required assertions; one is exercised, so the case as a whole is NOT_RUN.
    return [
        not_run(
            "P12",
            "A7",
            p,
            a,
            f"signer sub-assertion {sub}; lock-mismatch sub-assertion not exercisable: every image installs "
            "the same SDK code, and building a mismatched image would mean altering an owner's lock "
            "(outside this lane)",
            unauthorized_signer_subcase=sub,
            call=m,
            gate_log=glog,
            recovery=rec,
            sdk_code_digest=digests,
        )
    ]


def admin_register(body: dict, overwrite: bool = True) -> dict:
    tok = env()["GL_ADMIN_TOKEN"]
    r = sh(
        [
            "curl",
            "-sS",
            "--noproxy",
            "*",
            "--max-time",
            "20",
            "-o",
            "/dev/stdout",
            "-w",
            "\n%{http_code}",
            "-X",
            "POST",
            "-H",
            f"X-Admin-Token: {tok}",
            "-H",
            "Content-Type: application/json",
            f"http://127.0.0.1:19000/v1/admin/register?overwrite={'true' if overwrite else 'false'}",
            "--data-binary",
            json.dumps(body),
        ],
        timeout=30,
    )
    body_txt, _, code = r.stdout.rpartition("\n")
    return {"http_status": int(code) if code.isdigit() else None, "body": redact(body_txt[:400])}


GRAPH_REG = {
    "internal_url": "http://graph:8000",
    "supported_actions": ["match", "sync", "outcomes", "resolve", "health-assess", "score"],
    "priority_class": "P1",
    "max_concurrent": 100,
    "health_endpoint": "/v1/health",
    "timeout_ms": 30000,
    "metadata": {"owner": "ceg", "type": "engine"},
}


def c_registration(p: str) -> list[Result]:
    """A7 witness for F-A1-05 (registration not bound to the node key) + P11 owner collision."""
    collision = admin_register(
        {
            "gl-rogue-node": {
                **GRAPH_REG,
                "internal_url": "http://enrichment-engine:8000",
                "supported_actions": ["match"],
                "metadata": {"owner": "eie", "type": "engine"},
            }
        }
    )
    hijack: dict = {}
    during: dict = {}
    reg_hijacked: dict = {}
    glog: list[str] = []
    try:
        hijack = admin_register(
            {"graph": {**GRAPH_REG, "internal_url": "http://enrichment-engine:8000"}}
        )
        reg_hijacked = registry()
        during = drive("execute", MATCH_PROBE)
        glog = logs(
            "l9e2e-gate",
            "1m",
            ("worker_response", "GateDispatch", "worker_transport", "gate.failure"),
        )
    finally:
        restore = admin_register({"graph": GRAPH_REG})
    after = drive("execute", MATCH_PROBE)
    obs = {
        "owner_collision_register": collision,
        "hijack_register": hijack,
        "registry_after_hijack": reg_hijacked,
        "call_while_hijacked": during,
        "gate_log": glog,
        "restore_register": restore,
        "call_after_restore": {k: after.get(k) for k in ("ok", "error", "gate_signature_valid")},
    }
    witnessed = (
        hijack.get("http_status") in (200, 201)
        and during.get("ok") is False
        and after.get("ok") is True
    )
    collided = collision["http_status"] == 400 and "is owned by" in collision["body"]
    return [
        Result(
            "F-A1-05",
            "A7",
            p,
            "witness: an admin-token holder can repoint a canonical owner's registration; "
            "only response verification stops the mis-route",
            "WITNESSED" if witnessed else "NOT_WITNESSED",
            observed=obs,
            reason="BLOCKED_WITH_EXACT_OWNER: Constellation.Gate (registration not bound to the node key; "
            "admin token compared with != in services/admin_registration_service.py:64-68, source-level)",
        ),
        Result(
            "P11-owner-collision",
            "A7",
            p,
            "a registration claiming a canonical action under the wrong owner is refused",
            verdict(collided, p, signed_claim=False),
            observed={"owner_collision_register": collision},
        ),
    ]


def c_gate_rail_policy(p: str) -> list[Result]:
    """A7 witness for F-A1-11: Gate's own Docker rail config (staging, no caller policy) is refused by Gate settings."""
    e = env()
    probe = "\n".join(
        [
            "from constellation_gate.config.settings import get_settings",
            "try:",
            "    get_settings()",
            "    print('SETTINGS_OK')",
            "except Exception as exc:",
            "    print('SETTINGS_REFUSED', type(exc).__name__, str(exc)[:400])",
        ]
    )
    keys = json.dumps(
        {"gate-e2e": e["GL_HMAC_GATE"], "eie-e2e": e["GL_HMAC_EIE"], "ceg-e2e": e["GL_HMAC_CEG"]}
    )
    r = sh(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "-e",
            "GATE_LOCAL_NODE=gate",
            "-e",
            "L9_ENVIRONMENT=staging",
            "-e",
            "L9_DEV_MODE=false",
            "-e",
            "L9_REQUIRE_SIGNATURE=true",
            "-e",
            "L9_SIGNING_ALGORITHM=hmac-sha256",
            "-e",
            f"L9_SIGNING_KEY={e['GL_HMAC_GATE']}",
            "-e",
            "L9_SIGNING_KEY_ID=gate-e2e",
            "-e",
            f"L9_VERIFYING_KEYS_JSON={keys}",
            "-e",
            f"GATE_ADMIN_TOKEN={e['GL_ADMIN_TOKEN']}",
            "-e",
            "L9_REPLAY_ENABLED=true",
            "-e",
            "L9_TRUSTED_INGRESS_BOUNDARY=signature",
            "--entrypoint",
            "python",
            "l9e2e/gate:local",
            "-c",
            probe,
        ],
        timeout=120,
    )
    out = [redact(ln) for ln in (r.stdout + r.stderr).strip().splitlines()[-3:]]
    joined = " ".join(out)
    refused = "SETTINGS_REFUSED" in joined and "caller policy" in joined
    return [
        Result(
            "F-A1-11",
            "A7",
            p,
            "witness: the Gate rail's staging env (no L9_KEY_ALLOWED_ACTIONS_JSON) is "
            "refused by Gate settings; its driver signs as gate-e2e (run_e2e.sh drive(), source)",
            "WITNESSED" if refused else "NOT_WITNESSED",
            observed={"settings_probe": out},
            reason="BLOCKED_WITH_EXACT_OWNER: Constellation.Gate tests/e2e/docker (rail). This rail builds its "
            "own caller policy and separate consumer keys, so P02/P10/P11 do not depend on it.",
        )
    ]


def c_feedback(p: str) -> list[Result]:
    """F-A1-08 disposition: synthneutral changed-fact witness (P27, P22/P30 parts) and the plasticos justified no-op.

    The inference packet is sent with the ceg-gl key held by the driver (rig). CEG's own egress cannot sign
    under ed25519 at SDK bc1c74c (F-A1-04), and CEG's inference is reachable only through operator admin
    (F-A1-02). The rig output is exactly what infer_geography_from_postal_code returns for zip 94105.
    """
    eid = f"sn-fb-{uuid.uuid4().hex[:6]}"
    out = {
        "entity_id": eid,
        "field": "region",
        "value": "West",
        "confidence": 0.82,
        "rule": "infer_geography_from_postal_code",
        "provenance": {"rig": "driver ceg-gl", "postal_code": "94105"},
        "rationale": "US zip=94105",
    }
    accepted = drive(
        "execute",
        {
            "action": "graph-inference-result",
            "tenant": "synthneutral",
            "key_id": "ceg-gl",
            "idempotency_key": f"gl-fb-{eid}",
            "payload": {"inference_outputs": [out]},
        },
    )
    sch = {"region": "string", "band": "string"}
    c1 = drive(
        "execute", converge_payload(eid, "synthneutral", extra={"postal_code": "94105"}, schema=sch)
    )
    seeded_log = logs("l9e2e-eie", "1m", ("graph", eid))
    c2 = drive(
        "execute", converge_payload(eid, "synthneutral", extra={"postal_code": "94105"}, schema=sch)
    )
    f1, f2 = (
        (c1.get("payload") or {}).get("fields") or {},
        (c2.get("payload") or {}).get("fields") or {},
    )
    acc_ok = (accepted.get("payload") or {}).get("targets_queued") == 1
    consumed = f1.get("region") == "West"
    pid = f"gl-fbp-{uuid.uuid4().hex[:6]}"
    pacc = drive(
        "execute",
        {
            "action": "graph-inference-result",
            "tenant": "plasticos",
            "key_id": "ceg-gl",
            "idempotency_key": f"gl-fbp-{pid}",
            "payload": {"inference_outputs": [{**out, "entity_id": pid}]},
        },
    )
    pc = drive("execute", converge_payload(pid, "plasticos"))
    pfields = (pc.get("payload") or {}).get("fields") or {}
    noop = (
        (pacc.get("payload") or {}).get("targets_queued") == 1
        and bool(pfields)
        and "region" not in pfields
    )
    try:
        synced = cypher("synthneutral", f"MATCH (s:Station {{station_id:'{eid}'}}) RETURN s.region")
    except ProbeError as exc:
        synced = [["PROBE_ERROR", str(exc)]]
    obs = {
        "entity_id": eid,
        "accepted": accepted.get("payload"),
        "converge1_fields": f1,
        "converge2_fields": f2,
        "eie_log": seeded_log,
        "graph_after": synced,
        "plasticos_noop": {
            "entity_id": pid,
            "accepted": pacc.get("payload"),
            "fields": pfields,
            "observed": noop,
        },
    }
    p27 = Result(
        "P27",
        "A8",
        p,
        "accepted then consumed on a later pass, with distinct accepted/retained/consumed evidence",
        "BLOCKED" if acc_ok and consumed else "FAIL",
        observed=obs,
        reason=(
            "partial: accepted and consumed observed through Gate (consumed once; the next pass re-derives "
            "region from the provider). Retained has no observable evidence: in-memory only (F-A1-06, "
            "EIE-W). The inference came from the ceg-gl rig because CEG egress cannot sign (F-A1-04)"
        )
        if acc_ok and consumed
        else "accepted/consumed not both observed (see observed)",
    )
    p30 = Result(
        "P30",
        "A8",
        p,
        "graph inference reaches a later authorized EIE decision: changed fact + resync, or "
        "a justified no-op",
        "BLOCKED",
        observed=obs,
        reason="changed fact observed in the converge answer, but the resync cannot leave EIE under ed25519 "
        "(F-A1-04); plasticos justified no-op "
        + ("observed (region undeclared, absent from fields)" if noop else "NOT observed"),
    )
    p22 = Result(
        "P22",
        "A8",
        p,
        "material graph change with an authorized rule: CEG uses graph-read evidence and sends "
        "one cited finding",
        "BLOCKED",
        observed={
            "rule_declared": "synthneutral plugins.inference_rules",
            "trigger": "none Gate-routable",
        },
        reason="CEG inference only via operator admin (not a Gate action, not advertised) from the caller "
        "entity, not a graph read (F-A1-02, CEG-W); CEG egress cannot sign under ed25519 (F-A1-04)",
    )
    return [p27, p30, p22]


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
    "registration": c_registration,
    "gate_rail_policy": c_gate_rail_policy,
    "feedback": c_feedback,
}

# Ledger case ids each scenario produces; test_gate_loop.py derives its expectations from this map.
CASE_IDS: dict[str, tuple[str, ...]] = {
    "isolation": ("P03",),
    "policy": ("P10",),
    "negatives": ("P11", "P13"),
    "converge_plasticos": ("P07", "P18"),
    "converge_synthneutral": ("P05",),
    "sync_rejects": ("P17", "P19"),
    "match": ("P33", "P34"),
    "inference_dup": ("P23",),
    "ceg_down": ("P14",),
    "gate_restart": ("P09",),
    "rogue_signer": ("P12",),
    "registration": ("F-A1-05", "P11-owner-collision"),
    "gate_rail_policy": ("F-A1-11",),
    "feedback": ("P27", "P30", "P22"),
}


def sdk_digests() -> dict:
    """Code digest per process: sha256 over (relative path, file sha256) of every installed SDK .py file, sorted.

    Install metadata (RECORD, direct_url) is excluded, so identical code gives the same digest.
    """
    probe = "\n".join(
        [
            "import hashlib, importlib.metadata as m, pathlib, constellation_node_sdk as s",
            "root = pathlib.Path(s.__file__).parent",
            "fs = sorted(root.rglob('*.py'))",
            "h = hashlib.sha256()",
            "for f in fs:",
            "    h.update((str(f.relative_to(root)) + ':' + hashlib.sha256(f.read_bytes()).hexdigest() + '\\n').encode())",
            "print(m.version('constellation-node-sdk'), len(fs), h.hexdigest())",
        ]
    )
    out = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(["docker", "exec", c, "python", "-c", probe], timeout=60)
        out[c] = r.stdout.strip() or f"UNAVAILABLE rc={r.returncode}"
    return out


def release_set(profile: str) -> dict:
    """Provenance read from the RUNNING containers (their immutable image ids), not from mutable local tags."""
    imgs = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(["docker", "inspect", c, "--format", "{{.Image}}"], timeout=30)
        image_id = r.stdout.strip()
        if r.returncode != 0 or not image_id:
            raise ProbeError(f"cannot inspect running container {c}")
        lab = sh(
            [
                "docker",
                "image",
                "inspect",
                image_id,
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            ],
            timeout=30,
        )
        if lab.returncode != 0:
            raise ProbeError(f"cannot inspect the image of {c}")
        imgs[c] = {"image_id": image_id, "revision": lab.stdout.strip()}
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
        "requested_profile": profile,
        "observed_signing_algorithm": dict(OBSERVED_PROFILE),
        "images": imgs,
        "sdk_code_digest": sdk_digests(),
        "packs": packs,
        "provider_class": "deterministic (app/services/deterministic_provider.py, L9_ENVIRONMENT=test)",
        "registry": registry(),
    }


def write_json(path: Path, obj: object) -> None:
    path.write_text(redact(json.dumps(obj, indent=1, default=str)) + "\n")


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
    run_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    ev = STATE / "evidence" / f"{run_stamp}-{a.profile}"
    ev.mkdir(parents=True, exist_ok=True)
    if not a.no_boot:
        up = rail("up")
        if up.returncode != 0:
            write_json(ev / "boot.json", {"rc": up.returncode, "tail": up.stdout[-800:]})
            print(json.dumps({"evidence": str(ev), "boot": "FAILED"}))
            return 4
    OBSERVED_PROFILE.update(running_signing())
    write_json(ev / "ready.json", wait_ready())
    rs = release_set(a.profile)
    write_json(ev / "release_set.json", rs)
    results: list[Result] = []
    for name in a.cases.split(","):
        try:
            res = CASES[name](a.profile)
        except (
            Exception
        ) as exc:  # a harness failure is NOT_RUN, never PASS; raw exception text is never recorded
            why = (
                f"ProbeError: {redact(str(exc))[:200]}"
                if isinstance(exc, ProbeError)
                else type(exc).__name__
            )
            res = [
                not_run(cid, "A8", a.profile, "harness", why) for cid in CASE_IDS.get(name, (name,))
            ]
        for r in res:
            results.append(r)
            print(f"{r.id:5} {r.verdict:14} {r.reason[:100]}", flush=True)
            write_json(ev / f"{r.id}.json", asdict(r))
    leaks = [f.name for f in ev.glob("*.json") for s in secrets_() if s in f.read_text()]
    summary = {
        "stamp": run_stamp,
        "profile": a.profile,
        "observed_signing_algorithm": dict(OBSERVED_PROFILE),
        "release_set": rs,
        "leak_check": leaks or "clean",
        "results": [{"id": r.id, "verdict": r.verdict, "reason": r.reason} for r in results],
    }
    write_json(ev / "summary.json", summary)
    print(json.dumps({"evidence": str(ev), "leak_check": summary["leak_check"]}))
    return 0 if not leaks else 3


if __name__ == "__main__":
    sys.exit(main())
