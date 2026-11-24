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
import json
import os
import re
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
# Set by main() from the running containers; probes that start a container use these image ids.
RELEASE_SET: dict = {}


# ───────────────────────────── plumbing ─────────────────────────────


class ProbeError(RuntimeError):
    """A probe did not complete. The case must record NOT_RUN, never a result."""


class ProbeTimeoutError(ProbeError):
    """A probe ran out of its time budget (sh's own timeout), as opposed to failing."""


class StackUnusableError(ProbeError):
    """A case could not put the stack back (a container or registration it changed is not restored).

    Every later case would run against a modified stack, so the run stops: this case and every
    remaining one are NOT_RUN and the process exits non-zero.
    """


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
    keys += ["GL_ADMIN_TOKEN", "GL_NEO4J_SECRET", "GL_PG_SECRET"]
    return [e[k] for k in keys if k in e and len(e[k]) > 8]


def redact(text: str) -> str:
    for s in secrets_():
        text = text.replace(s, "[REDACTED]")
    return text


SH_TIMEOUT_RC = 124
SH_TIMEOUT_MARK = "timed out after"


def sh(
    cmd: list[str], *, timeout: float = 600, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    """Run a command. A timeout returns rc 124 and a message that names no argument."""
    e = {**os.environ, **(extra_env or {})}
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=e)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd[:1], SH_TIMEOUT_RC, "", f"{cmd[0]}: {SH_TIMEOUT_MARK} {timeout:g}s"
        )


def rail(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return sh(["bash", str(HERE / "scripts/rail.sh"), *args], extra_env=extra_env, timeout=1200)


def drive(scenario: str, args: dict | None = None) -> dict:
    r = sh(["bash", str(HERE / "scripts/drive.sh"), scenario, json.dumps(args or {})], timeout=600)
    if r.returncode != 0:
        return {
            "driver_error": f"launcher rc {r.returncode}: {redact(r.stderr or r.stdout)[-600:]}"
        }
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"driver_error": redact(r.stderr or r.stdout)[-600:]}


def cypher(db: str, query: str, timeout: float = 120) -> list[list[str]]:
    """Rows of a Cypher query (header dropped). Raises ProbeError when the query does not complete."""
    pw = env()["GL_NEO4J_SECRET"]
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
        timeout=timeout,
    )
    if r.returncode != 0:
        msg = f"cypher on {db} failed (rc {r.returncode}): {redact(r.stderr.strip())[-300:]}"
        if r.returncode == SH_TIMEOUT_RC and SH_TIMEOUT_MARK in r.stderr:
            raise ProbeTimeoutError(msg)
        raise ProbeError(msg)
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return [ln.split(", ") for ln in lines[1:]]


def psql(query: str) -> list[str]:
    r = sh(
        ["docker", "exec", "l9e2e-eie-pg", "psql", "-U", "enrich", "-d", "enrich", "-Atc", query],
        timeout=120,
    )
    if r.returncode != 0:
        raise ProbeError(f"psql failed (rc {r.returncode}): {redact(r.stderr.strip())[-300:]}")
    return r.stdout.strip().splitlines()


def logs(
    container: str, since: str = "2m", grep: tuple[str, ...] = (), limit: int | None = 40
) -> list[str]:
    """Container log lines since ``since``, optionally filtered; the last ``limit`` (None: all)."""
    r = sh(["docker", "logs", container, "--since", since], timeout=60)
    if r.returncode != 0:
        raise ProbeError(
            f"docker logs {container} failed (rc {r.returncode}): {redact(r.stderr.strip())[-300:]}"
        )
    lines = (r.stdout + r.stderr).splitlines()
    lines = [ln for ln in lines if "otel" not in ln and "opentelemetry" not in ln]
    if grep:
        lines = [ln for ln in lines if any(g in ln for g in grep)]
    if limit is not None:
        lines = lines[-limit:]
    return [redact(ln) for ln in lines]


def gate_events(since: str, action: str) -> list[dict]:
    """Gate's structured ingress/completion events for one action since a docker-logs timestamp."""
    out = []
    for ln in logs("l9e2e-gate", since, (f'"action": "{action}"',), limit=None):
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


def gate_events_for(since: str, packet_id: str) -> list[dict]:
    """Gate's structured events for one request (exact correlation, not a time window).

    Ingress and failure events carry the request's packet_id. completed and cached events carry the
    response's packet_id and the request's root_id, so a request is matched on either field.
    """
    keys = ("event", "action", "request_id", "packet_id", "root_id", "error_type", "error_message")
    out = []
    for ln in logs("l9e2e-gate", since, (packet_id,)):
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if ev.get("event") and packet_id in (ev.get("packet_id"), ev.get("root_id")):
            out.append({k: ev.get(k) for k in keys})
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


def registry_raw() -> dict | None:
    """Gate's registry as returned (node name -> registration), or None when it cannot be read."""
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
        return None
    return d if isinstance(d, dict) else None


def registry() -> dict:
    d = registry_raw()
    if d is None:
        return {"unavailable": True}
    return {k: sorted(v.get("supported_actions", [])) for k, v in d.items()}


def registry_url(node: str) -> str | None:
    d = registry_raw() or {}
    return (d.get(node) or {}).get("internal_url")


def stamp() -> str:
    """A docker-logs --since value meaning 'from now on'."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def running_signing() -> dict:
    """The signing algorithm each container was started with, or NOT_RUNNING / UNAVAILABLE."""
    out = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(
            [
                "docker",
                "inspect",
                c,
                "--format",
                "running={{.State.Running}}\n{{range .Config.Env}}{{println .}}{{end}}",
            ],
            timeout=30,
        )
        lines = r.stdout.splitlines()
        alg = next(
            (ln.split("=", 1)[1] for ln in lines if ln.startswith("L9_SIGNING_ALGORITHM=")),
            None,
        )
        if r.returncode != 0 or not alg:
            out[c] = "UNAVAILABLE"
        elif "running=true" not in lines:
            out[c] = "NOT_RUNNING"
        else:
            out[c] = alg
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
    """PASS only on the target profile as observed in the running containers at verdict time.

    OBSERVED_PROFILE is the startup record; every PASS re-reads the containers after the case's
    last lifecycle action, so a container that was recreated or stopped cannot inherit it.
    """
    del signed_claim  # every claim needs the observed target profile for PASS
    if not ok:
        return "FAIL"
    if profile != "ed25519" or set(OBSERVED_PROFILE.values()) != {"ed25519"}:
        return "DIAGNOSTIC_OK"
    now = running_signing()
    if set(now.values()) != {"ed25519"}:
        raise ProbeError(f"signing profile at verdict time is not ed25519 x3: {now}")
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


def facility(db: str, fid: str, timeout: float = 120) -> dict:
    """Facility read-back. valueType() is a Neo4j 5 built-in (no APOC). Raises ProbeError on query failure."""
    rows = cypher(
        db,
        f"MATCH (f:Facility {{facility_id:'{fid}'}}) "
        "OPTIONAL MATCH (f)-[r:PROCESSES]->(p:PolymerFamily) "
        "RETURN f.facility_id, f.name, f.contamination_tolerance, p.code, r.line_id, r.direction, "
        "r.form, valueType(f.processed_polymers)",
        timeout=timeout,
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

    def reached(k: str, act: str, worker: str) -> bool:
        """A response packet came back and its source is the expected worker (a typed failure counts)."""
        c = cells[(k, act)]
        return c.get("reached_worker") is True and c.get("response_source") == worker

    checks = {
        "eie worker score: kind rule": kind_refused("eie-gl", "score"),
        "ceg worker score: kind rule": kind_refused("ceg-gl", "score"),
        "ceg worker health-assess: kind rule": kind_refused("ceg-gl", "health-assess"),
        "eie worker health-assess: reaches graph": reached("eie-gl", "health-assess", "graph"),
        "operator consumer health-assess: reaches graph (WORKER_ORIGINATED applies to workers only)": reached(
            "op-gl", "health-assess", "graph"
        ),
        "odoo consumer health-assess: refused by its policy": policy_refused(
            "odoo-gl", "health-assess"
        ),
        "ceg worker graph-inference-result: reaches enrichment-engine": reached(
            "ceg-gl", "graph-inference-result", "enrichment-engine"
        ),
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
    since = stamp()
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
    base = n.get("baseline_valid") or {}
    control_ok = (
        base.get("http_status") == 200
        and base.get("gate_signature_valid") is True
        and (base.get("payload") or {}).get("status") == "success"
    )
    first, second = n.get("replay_first") or {}, n.get("replay_second_same_packet_id") or {}
    qid1 = (first.get("payload") or {}).get("query_id")
    rpid = str(n.get("replay_packet_id") or "")
    replay_events = gate_events_for(since, rpid) if rpid else []
    dispatched = [e for e in replay_events if e["event"] == "gate.completed"]
    cached = [e for e in replay_events if e["event"] == "gate.cached"]
    resp1 = (first.get("response") or {}).get("packet_id")
    resp2 = (second.get("response") or {}).get("packet_id")
    s2 = st.get("replay_second_same_packet_id")
    if s2 == 400:
        replay_outcome = "refused"
    elif (
        s2 == 200
        and second.get("payload") == first.get("payload")
        and resp1 is not None
        and resp1 == resp2
        and len(dispatched) == 1
        and len(cached) == 1
        and cached[0]["packet_id"] == resp1
    ):
        # Gate executed the packet id once (one gate.completed) and answered the replay from its
        # cache (gate.cached) with the same response packet: no second execution.
        replay_outcome = "cache_hit"
    else:
        replay_outcome = "unproven"
    checks13 = {
        "first execution succeeded with a query_id": st.get("replay_first") == 200 and bool(qid1),
        "same packet id: replay refused, or an identical answer with a single Gate dispatch": (
            replay_outcome in ("refused", "cache_hit")
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
        observed={
            "http_status": st,
            "checks": checks13,
            "replay_outcome": replay_outcome,
            "replay_packet_id": rpid,
            "replay_gate_events": replay_events,
        },
        reason=("failed: " + "; ".join(failed13))
        if failed13
        else {
            "refused": "Gate refused the replayed packet id (HTTP 400): no second execution",
            "cache_hit": "Gate answered the replayed packet id from its cache (gate.cached, the same "
            "response packet and payload) after a single gate.completed: no second execution",
        }[replay_outcome],
    )
    if not control_ok:
        r11 = not_run(
            "P11",
            "A7",
            p,
            r11.assertion,
            "positive control failed: the valid signed match did not return a Gate-verified success, "
            "so the denials are not evidence",
            baseline_valid=base,
            http_status=st,
        )
    return [r11, r13]


TYPED_SYNC_BLOCKED = (
    "BLOCKED pending an operator contract decision (the sync row carries only admitted fields, "
    "by design since #351)"
)

# Gate's packet log context (observability/logging.py:37-47) carries no entity field, so the rail
# cannot bind a Gate sync event to one entity. It binds each case by EIE's own graph-leg record
# for the run's unique entity id plus the CEG row, and by request_id between Gate's ingress and
# completion; Gate's events prove that a sync from EIE completed in the window, not which entity.
GATE_BINDING_LIMIT = (
    "Gate's sync events carry no entity field (observability/logging.py:37-47); per-entity binding "
    "is EIE's graph-leg record plus the CEG row, not Gate's events"
)


# EIE's signing-error line has no entity id, so it may belong to another converge in the window.
EGRESS_SIGNING_UNCORRELATED = (
    "EIE logged 'signature required but not present' for a graph sync in this window while this "
    "entity's graph leg resolved unsynced; the line carries no entity id, so it is not attributed "
    "to this entity (F-A1-04 not asserted)"
)


def typed_sync_blocked(
    *, stored: bool, sync_via_gate: bool, typed_edges: bool, other_requirements_met: bool
) -> bool:
    """True only when the missing typed edge is the sole failure of a typed-sync case.

    BLOCKED pending the operator contract applies when the row reached CEG through Gate and
    every other requirement of the case holds. Any other failure stays FAIL (rule 95).
    """
    return stored and sync_via_gate and other_requirements_met and not typed_edges


def graph_return_targets(entity_id: str) -> list[dict]:
    """EIE's durable graph-return targets for one synthneutral entity (retained -> consuming -> consumed)."""
    rows = psql(
        "select state, coalesce(consumed_by, ''), coalesce(outcome, ''), created_at "
        "from graph_return_targets "
        f"where tenant_id='synthneutral' and entity_id='{entity_id}' order by created_at"
    )
    out = []
    for row in rows:
        state, consumed_by, outcome, created_at = (row.split("|") + ["", "", "", ""])[:4]
        out.append(
            {
                "state": state,
                "consumed_by": consumed_by,
                "outcome": outcome,
                "created_at": created_at,
            }
        )
    return out


def graph_leg_outcomes(eie_log: list[str], entity_id: str) -> list[bool]:
    """The synced flag of every side_effects_graph_leg_resolved record EIE logged for this entity."""
    out = []
    for line in eie_log:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(rec, dict)
            and rec.get("event") == "side_effects_graph_leg_resolved"
            and rec.get("entity_id") == entity_id
        ):
            out.append(rec.get("synced") is True)
    return out


def graph_leg_synced(eie_log: list[str], entity_id: str) -> bool:
    """EIE recorded its graph leg for this entity as synced (side_effects_graph_leg_resolved)."""
    return True in graph_leg_outcomes(eie_log, entity_id)


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
    # Verdict evidence from the case start, untruncated: a probe delay or later traffic must not
    # push this entity's graph-leg record out of the window. Only the recorded excerpt is capped.
    eie_all = logs(
        "l9e2e-eie",
        since,
        ("graph_sync", "packet_route_error", "graph_leg", "side_effects"),
        limit=None,
    )
    eie_log = eie_all[-40:]
    # Bound to this converge by its entity, not by lineage: EIE starts every Gate egress as a new
    # root packet (app/engines/packet_router.py route(): "The SDK builds the root packet"), so the
    # sync never shares the converge's root_id. The binding is EIE's own graph-leg record for this
    # run's unique entity id, plus Gate's sync from EIE completed by the graph worker in this window.
    leg_synced = graph_leg_synced(eie_all, eid)
    ingress = [
        e
        for e in sync_events
        if e["event"] == "gate.ingress" and e["source_node"] == "enrichment-engine"
    ]
    completed = [
        e
        for e in sync_events
        if e["event"] == "gate.completed"
        and e["source_node"] == "graph"
        and any(e["request_id"] == i["request_id"] for i in ingress)
    ]
    resp = r.get("response") or {}
    converge_lineage = (
        resp.get("signing_key_id") == "gate-gl"
        and r.get("gate_signature_valid") is True
        and resp.get("generation", 0) >= 2
        and resp.get("causation_id") is not None
    )
    sync_via_gate = (
        leg_synced
        and bool(ingress)
        and bool(completed)
        and all((c.get("generation") or 0) >= 2 for c in completed)
    )
    # EIE's signing-error line (packet_route_error) carries no entity, request or packet id, and an
    # unsynced graph leg has other causes (a CEG refusal, for one), so the error cannot be tied to
    # this entity. It is recorded as an uncorrelated observation; it never changes the verdict.
    egress_signing_error_seen = False in graph_leg_outcomes(eie_all, eid) and any(
        "signature required but not present" in ln for ln in eie_all
    )
    state = (r.get("payload") or {}).get("state")
    obs = {
        "entity_id": eid,
        "converge": {k: r.get(k) for k in ("ok", "elapsed_s", "error", "gate_signature_valid")},
        "response": resp,
        "state": state,
        "eie_persisted_rows": persisted,
        "graph": g,
        "gate_sync_events": sync_events,
        "eie_graph_leg_synced": leg_synced,
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
                reason=(
                    GATE_BINDING_LIMIT
                    if ok07
                    else f"sync not observed through Gate, or row not read back; {GATE_BINDING_LIMIT}"
                ),
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
                reason=GATE_BINDING_LIMIT
                if stored
                else f"no row reached CEG; {GATE_BINDING_LIMIT}",
            )
        )
    # The node arrived through Gate but no typed child edge did: EIE's sync row carries only the
    # admitted (enriched) fields by design since #351, which leaves the edge contract to an operator.
    typed_ids = ("P18",) if tenant == "plasticos" else ("P05",)
    other_ok = not_stringified if tenant == "plasticos" else state == "completed"
    if typed_sync_blocked(
        stored=stored,
        sync_via_gate=sync_via_gate,
        typed_edges=typed_edges,
        other_requirements_met=other_ok,
    ):
        for res in out:
            if res.id in typed_ids:
                res.verdict = "BLOCKED"
                # The operator-contract wording first, then the limit of the Gate evidence that
                # sync_via_gate rests on, which this reason must not drop.
                res.reason = f"{TYPED_SYNC_BLOCKED}; {GATE_BINDING_LIMIT}"
    if not stored and egress_signing_error_seen:
        for res in out:
            res.reason = f"{res.reason}; {EGRESS_SIGNING_UNCORRELATED}"
    return out


def _started_at(container: str) -> str:
    """The container's State.StartedAt; empty when it cannot be inspected."""
    r = sh(["docker", "inspect", container, "--format", "{{.State.StartedAt}}"], timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def c_ceg_down(p: str) -> list[Result]:
    """P14: EIE completes while CEG is down; durable result; the pending graph intent survives an EIE restart."""
    a = "EIE completes while CEG is down; durable result; pending graph intent recovered after EIE restart"
    eid = f"gl-down-{uuid.uuid4().hex[:6]}"
    case_since = stamp()
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
        intent_down = graph_sync_intents(eid)
        started_before = _started_at("l9e2e-eie")
        restart = sh(["docker", "restart", "l9e2e-eie"], timeout=180)
        started_after = _started_at("l9e2e-eie")
        if (
            restart.returncode != 0
            or not started_before
            or not started_after
            or started_after == started_before
        ):
            raise ProbeError(
                f"EIE restart not proven (rc {restart.returncode}, "
                f"StartedAt {started_before} -> {started_after})"
            )
    except ProbeError as exc:
        return [not_run("P14", "A8", p, a, str(exc), entity_id=eid)]
    finally:
        start = sh(["docker", "start", "l9e2e-ceg"], timeout=120)
        ready = wait_ready()
        if start.returncode != 0 or not ready.get("ready"):
            raise StackUnusableError(
                f"CEG did not come back after the outage case (start rc {start.returncode}, {ready})"
            )
    # EIE's recovery pass runs 30 s after startup and then every 30 s, and an in-flight intent is
    # held by its 60 s lease, so recovery is polled for up to 120 s rather than read once.
    try:
        g = poll_recovered_facility(eid, budget_s=120)
        intent_after = graph_sync_intents(eid)
    except ProbeError as exc:
        return [not_run("P14", "A8", p, a, str(exc), entity_id=eid)]
    # The recovery record carries only an attempted count (side_effect_coordinator.py), not the entity:
    # it is evidence that a recovery pass ran in this case's window; the graph row binds the entity.
    recovered_log = logs("l9e2e-eie", case_since, ("side_effects_graph_recovered",), limit=None)
    st = (r.get("payload") or {}).get("state")
    ok = (
        st == "completed"
        and persisted == ["1"]
        and absent_before
        and bool(intent_down)
        and g["facility_count"] == 1
    )
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
                "eie_restart": {"started_before": started_before, "started_after": started_after},
                "graph_after_recovery": g,
                "graph_sync_intents_while_down": intent_down,
                "graph_sync_intents_after_recovery": intent_after,
                "eie_side_effects_graph_recovered": recovered_log,
            },
            reason="" if ok else _p14_reason(st, persisted, absent_before, g, intent_down),
        )
    ]


def poll_recovered_facility(entity_id: str, budget_s: float) -> dict:
    """Poll the graph for the entity until it appears or the budget ends.

    Every probe gets at most the remaining budget, the wait between probes never outlasts it, and a
    row read after the deadline does not count, so the reported window is the observed one. A
    probe that times out at the deadline ends the poll; any other failure, or an early timeout, raises.
    """
    deadline = time.monotonic() + budget_s
    g: dict = {"facility_count": None, "within_s": budget_s}
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            # The exact remaining budget, fractional: truncating it would time the probe out
            # before the deadline, and a floor would let it run past one.
            got = facility("plasticos", entity_id, timeout=remaining)
        except ProbeTimeoutError:
            # Only running out of the budget ends the poll; any other probe failure, at the
            # deadline or not, raises so the case records NOT_RUN rather than a verdict.
            if time.monotonic() >= deadline:
                break
            raise
        if time.monotonic() > deadline:
            break
        g = {**got, "within_s": budget_s}
        if g["facility_count"] == 1:
            break
        time.sleep(min(10.0, max(0.0, deadline - time.monotonic())))
    return g


def graph_sync_intents(entity_id: str) -> list[str]:
    """EIE's durable graph-sync intents for one entity, as 'state|attempts|lease_expires_at' rows."""
    return psql(
        "select state, attempts, lease_expires_at from graph_sync_intents "
        f"where entity_id='{entity_id}'"
    )


def _p14_reason(
    state: str | None, persisted: list[str], absent: bool, graph: dict, intents: list[str]
) -> str:
    """The P14 failure reason, from what was observed rather than a fixed string."""
    parts = []
    if state != "completed":
        parts.append(f"converge state {state!r}, not completed, while CEG was down")
    if persisted != ["1"]:
        parts.append(f"EIE durable result rows {persisted}, not exactly one")
    if not absent:
        parts.append("the row was already in the graph while CEG was down (outage not effective)")
    if not intents:
        parts.append("no graph_sync_intents row for the entity while CEG was down")
    if graph.get("facility_count") != 1:
        parts.append(
            f"graph rows within 120 s of CEG recovery and an EIE restart: "
            f"{graph.get('facility_count')}, not 1"
        )
    return "; ".join(parts)


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

    Assertions are scoped to the facility ids this case seeds. Rows other cases wrote into the same tenant database can
    also match; they are recorded under off_fixture_candidates and never asserted on.
    """
    vec = json.loads((HERE / "vectors/sync_rows.json").read_text())
    rows = [v["row"] for v in vec["vectors"] if v["id"].startswith("PX-POS")]
    # The fixture set is exactly what this case seeds; any other returned id (rows written by
    # other cases, e.g. converge_plasticos's tp-fac-010) is recorded, never asserted on.
    seeded = {r["facility_id"] for r in rows}
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
            "fixture_ids": sorted(i for i in ids if i in seeded),
            "off_fixture_candidates": sorted(str(i) for i in ids if i not in seeded),
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
    started_before = _started_at("l9e2e-gate")
    restart = sh(["docker", "restart", "l9e2e-gate"], timeout=120)
    started_after = _started_at("l9e2e-gate")
    if (
        restart.returncode != 0
        or not started_before
        or not started_after
        or started_after == started_before
    ):
        return [
            not_run(
                "P09",
                "A7",
                p,
                a,
                "Gate restart not proven",
                restart_rc=restart.returncode,
                started_before=started_before,
                started_after=started_after,
            )
        ]
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
    if "driver_error" in m:
        return [not_run("P09", "A7", p, a, "the outage call returned no driver result", **obs)]
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


def _verify_restored_graph() -> dict:
    """Stop the run unless l9e2e-ceg runs the startup-verified image and the pinned SDK; return digests."""
    digests = sdk_digests()
    pinned = (RELEASE_SET.get("sdk_pin") or {}).get("expected")
    graph_image = sh(["docker", "inspect", "l9e2e-ceg", "--format", "{{.Image}}"], timeout=30)
    verified_image = ((RELEASE_SET.get("images") or {}).get("l9e2e-ceg") or {}).get("image_id")
    if (
        not pinned
        or not verified_image
        or graph_image.returncode != 0
        or graph_image.stdout.strip() != verified_image
        or digests.get("l9e2e-ceg") != pinned
    ):
        raise StackUnusableError(
            "restored graph worker is not the verified image/SDK "
            f"(image {graph_image.stdout.strip()[:19]} vs {str(verified_image)[:19]}, "
            f"sdk {digests.get('l9e2e-ceg')})"
        )
    return digests


def _restore_trusted_graph() -> tuple[dict, dict]:
    """Recreate the trusted graph worker and verify it; any failure here stops the run.

    Runs on every exit of c_rogue_signer, including a setup or probe error: later cases may run only
    on the image (and so the SDK) verified at startup, and the recreation resolves the service from its
    tag. An exception while restoring or verifying (docker unavailable, timeout) means provenance
    cannot be established, so it becomes StackUnusableError too.
    """
    try:
        back = rail("compose", "up", "-d", "--no-deps", "--force-recreate", "graph")
        rec = wait_ready()
        if back.returncode != 0 or not rec.get("ready"):
            raise StackUnusableError(
                f"trusted graph worker not restored after the rogue-signer case (rc {back.returncode}, {rec})"
            )
        return rec, _verify_restored_graph()
    except StackUnusableError:
        raise
    except Exception as exc:
        msg = f"graph worker restoration could not be verified ({type(exc).__name__})"
        raise StackUnusableError(msg) from exc


def c_rogue_signer(p: str) -> list[Result]:
    """P12: a worker response signed by a key Gate does not trust is refused; SDK code digest identical."""
    a = "SDK lock mismatch blocked AND untrusted response signer refused"
    m: dict = {}
    events: list[dict] = []
    try:
        setup = rail(
            "compose",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "graph",
            extra_env={"GL_ROGUE_CEG": "1"},
        )
        if setup.returncode != 0:
            raise ProbeError(f"rogue graph worker could not be created (rc {setup.returncode})")
        time.sleep(25)
        since = stamp()
        m = drive("post_signed", MATCH_PROBE)
        rpid = str(m.get("request_packet_id") or "")
        events = gate_events_for(since, rpid) if rpid else []
    finally:
        rec, digests = _restore_trusted_graph()
    pinned = (RELEASE_SET.get("sdk_pin") or {}).get("expected")
    valid = [v for v in digests.values() if not v.startswith("UNAVAILABLE")]
    # Identical across processes AND equal to the release the run is judged at.
    same = len(valid) == 3 and set(valid) == {pinned}
    # Refusal tied to this request: Gate's own gate.failure for its packet id, naming the response check.
    untrusted = m.get("http_status") == 502 and any(
        e["event"] == "gate.failure"
        and e["error_type"] == "GateDispatchSecurityError"
        and "worker response failed transport validation" in str(e["error_message"])
        for e in events
    )
    if len(valid) != 3:
        sub = "NOT_RUN (SDK code digest unavailable)"
    else:
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
            gate_events_for_request=events,
            recovery=rec,
            sdk_code_digest=digests,
        )
    ]


# The 502 a MIX-ALG-01 PASS records is expected, and its cause is what the case proves.
MIX_ALG_ACCEPTED_REASON = (
    "the hmac-sha256 worker verified Gate's ed25519 dispatch and answered: Gate's failure is its own "
    "response check (GateDispatchSecurityError, worker response failed transport validation), which "
    "is reachable only after the worker accepted the dispatch. The 502 is expected: Gate binds every "
    "verifying key to one algorithm, so an hmac worker answer is outside Gate's contract. A worker "
    "refusal would be HTTP 401/403 (FAIL)"
)


def mixed_alg_outcome(http_status: object, events: list[dict]) -> str:
    """Classify one Gate->worker call made while the worker signs hmac-sha256 and Gate ed25519.

    accepted: the worker verified Gate's ed25519 dispatch and answered; Gate then refused the answer at
    its own response check (Gate binds every verifying key to one algorithm, so an hmac worker answer
    is outside its contract). refused: the worker rejected Gate's dispatch (HTTP 401/403), which is
    the Gate_SDK D-1 regression seen in 1.3.3 (every verifying key bound to the node's own algorithm;
    repaired in 1.3.4, 8e8bf37).
    """
    if http_status != 502:
        return "unproven"
    for e in events:
        if e.get("event") != "gate.failure":
            continue
        msg = str(e.get("error_message"))
        if e.get("error_type") == "WorkerHTTPError" and re.search(r"returned HTTP 40[13]", msg):
            return "refused"
        if (
            e.get("error_type") == "GateDispatchSecurityError"
            and "worker response failed transport validation" in msg
        ):
            return "accepted"
    return "unproven"


def c_mixed_alg(p: str) -> list[Result]:
    """MIX-ALG-01: a worker signing hmac-sha256 still verifies Gate's ed25519 dispatch."""
    a = "an hmac-sha256 worker verifies Gate's ed25519 dispatch (per-key algorithm, not the node's)"
    m: dict = {}
    events: list[dict] = []
    window: dict = {}
    try:
        setup = rail(
            "compose",
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "graph",
            extra_env={"GL_MIXED_CEG": "1"},
        )
        if setup.returncode != 0:
            raise ProbeError(f"hmac graph worker could not be created (rc {setup.returncode})")
        time.sleep(25)
        window = running_signing()
        if window.get("l9e2e-ceg") != "hmac-sha256" or window.get("l9e2e-gate") != "ed25519":
            raise ProbeError(f"mixed-algorithm window not established: {window}")
        since = stamp()
        m = drive("post_signed", MATCH_PROBE)
        rpid = str(m.get("request_packet_id") or "")
        events = gate_events_for(since, rpid) if rpid else []
    finally:
        rec, digests = _restore_trusted_graph()
    outcome = mixed_alg_outcome(m.get("http_status"), events)
    obs = {
        "signing_in_window": window,
        "call": m,
        "gate_events_for_request": events,
        "outcome": outcome,
        "recovery": rec,
        "sdk_code_digest": digests,
    }
    if outcome == "unproven":
        return [not_run("MIX-ALG-01", "A7", p, a, "outcome not tied to this request", **obs)]
    return [
        Result(
            "MIX-ALG-01",
            "A7",
            p,
            a,
            verdict(outcome == "accepted", p, signed_claim=True),
            observed=obs,
            reason=(
                MIX_ALG_ACCEPTED_REASON
                if outcome == "accepted"
                else "the hmac worker refused Gate's ed25519 dispatch (owner Gate_SDK: a peer key bound to "
                "the node's own algorithm, the D-1 regression seen in 1.3.3)"
            ),
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
    events: list[dict] = []
    try:
        hijack = admin_register(
            {"graph": {**GRAPH_REG, "internal_url": "http://enrichment-engine:8000"}}
        )
        reg_hijacked = registry()
        since = stamp()
        during = drive("post_signed", MATCH_PROBE)
        dpid = str(during.get("request_packet_id") or "")
        events = gate_events_for(since, dpid) if dpid else []
    finally:
        restore = admin_register({"graph": GRAPH_REG})
        restored = registry()
        if restore.get("http_status") not in (200, 201) or restored.get("graph") != sorted(
            GRAPH_REG["supported_actions"]
        ):
            raise StackUnusableError(
                f"graph registration not restored (HTTP {restore.get('http_status')}); stop using this stack"
            )
        if registry_url("graph") != GRAPH_REG["internal_url"]:
            raise StackUnusableError(
                "graph registration restored with the wrong internal_url; stop using this stack"
            )
    after = drive("execute", MATCH_PROBE)
    obs = {
        "owner_collision_register": collision,
        "hijack_register": hijack,
        "registry_after_hijack": reg_hijacked,
        "call_while_hijacked": during,
        "gate_events_for_request": events,
        "restore_register": restore,
        "call_after_restore": {k: after.get(k) for k in ("ok", "error", "gate_signature_valid")},
    }
    # Witness: Gate dispatched this very request to the replacement, and the worker there refused it.
    dispatched_to_replacement = during.get("http_status") == 502 and any(
        e["event"] == "gate.failure"
        and e["error_type"] == "WorkerHTTPError"
        and "worker 'graph' execute returned HTTP 403" in str(e["error_message"])
        for e in events
    )
    witnessed = (
        hijack.get("http_status") in (200, 201)
        and dispatched_to_replacement
        and after.get("ok") is True
    )
    collided = collision["http_status"] == 400 and "is owned by" in collision["body"]
    return [
        Result(
            "F-A1-05",
            "A7",
            p,
            "witness: an admin-token holder can repoint a canonical owner's registration; the "
            "replacement is accepted, then the worker rejects the mis-routed packet (403, Gate 502)",
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
    # The probe runs the exact Gate image recorded in the release set, never a mutable local tag.
    gate_image = ((RELEASE_SET.get("images") or {}).get("l9e2e-gate") or {}).get("image_id")
    if not gate_image:
        raise ProbeError("no Gate image id in the release set; the settings probe cannot run")
    probe = "\n".join(
        [
            "import sys",
            "from constellation_gate.config.settings import get_settings",
            "try:",
            "    get_settings()",
            "    sys.stdout.write('SETTINGS_OK\\n')",
            "except Exception as exc:",
            "    sys.stdout.write(' '.join(['SETTINGS_REFUSED', type(exc).__name__, str(exc)[:400]]) + '\\n')",
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
            gate_image,
            "-c",
            probe,
        ],
        timeout=120,
    )
    out = [redact(ln) for ln in (r.stdout + r.stderr).strip().splitlines()[-3:]]
    joined = " ".join(out)
    if r.returncode != 0 or not ("SETTINGS_REFUSED" in joined or "SETTINGS_OK" in joined):
        return [
            not_run(
                "F-A1-11",
                "A7",
                p,
                "settings probe",
                f"the settings probe did not run to a verdict (rc {r.returncode})",
                settings_probe=out,
                gate_image_id=gate_image,
            )
        ]
    refused = "SETTINGS_REFUSED" in joined and "caller policy" in joined
    return [
        Result(
            "F-A1-11",
            "A7",
            p,
            "witness: the Gate rail's staging env (no L9_KEY_ALLOWED_ACTIONS_JSON) is "
            "refused by Gate settings; its driver signs as gate-e2e (run_e2e.sh drive(), source)",
            "WITNESSED" if refused else "NOT_WITNESSED",
            observed={"settings_probe": out, "gate_image_id": gate_image},
            reason="BLOCKED_WITH_EXACT_OWNER: Constellation.Gate tests/e2e/docker (rail). This rail builds its "
            "own caller policy and separate consumer keys, so P02/P10/P11 do not depend on it.",
        )
    ]


def c1_consumed_rows(targets_after_c1: list[dict]) -> list[dict]:
    """Targets the first converge consumed (consumed_by converge:<op>, outcome seeded).

    Only the state read right after c1 counts: a target the second converge consumed is not
    evidence that c1, whose answer P27 judges, consumed it.
    """
    return [
        t
        for t in targets_after_c1
        if t["state"] == "consumed"
        and t["consumed_by"].startswith("converge:")
        and t["outcome"] == "seeded"
    ]


def p27_requirements(
    *, accepted: bool, converge1_completed: bool, consumed: bool, retained: bool, consumed_row: bool
) -> dict[str, bool]:
    """Every P27 requirement by name; P27 passes only when all hold.

    The consume must come from a converge that completed: a region read from a failed or
    partial converge answer is not a consume.
    """
    return {
        "accepted (targets_queued 1)": accepted,
        "converge completed": converge1_completed,
        "consumed (converge region=West)": consumed,
        "retained row after intake": retained,
        "consumed row (consumed_by converge:<op>, outcome seeded)": consumed_row,
    }


def c_feedback(p: str) -> list[Result]:
    """F-A1-08 disposition: synthneutral changed-fact witness (P27, P30 part) and the plasticos justified no-op.

    The inference packet is sent with the ceg-gl key held by the driver (rig): CEG's inference is reachable
    only through operator admin, not a Gate action (F-A1-02). The rig output is exactly what
    infer_geography_from_postal_code returns for zip 94105. P30's resync is read from the graph right after
    the converge that consumed the fact, before the next pass re-derives it.
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
    targets_intake = graph_return_targets(eid)
    c1 = drive(
        "execute", converge_payload(eid, "synthneutral", extra={"postal_code": "94105"}, schema=sch)
    )
    seeded_log = logs("l9e2e-eie", "1m", ("graph", eid))
    targets_after_c1 = graph_return_targets(eid)
    time.sleep(8)
    try:
        after_c1 = cypher(
            "synthneutral", f"MATCH (s:Station {{station_id:'{eid}'}}) RETURN s.region"
        )
    except ProbeError as exc:
        after_c1 = [["PROBE_ERROR", str(exc)]]
    c2 = drive(
        "execute", converge_payload(eid, "synthneutral", extra={"postal_code": "94105"}, schema=sch)
    )
    targets_after_c2 = graph_return_targets(eid)
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

    def _converged(r: dict) -> bool:
        return r.get("ok") is True and (r.get("payload") or {}).get("state") == "completed"

    c1_ok, pc_ok = _converged(c1), _converged(pc)
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
        "graph_return_targets": {
            "after_intake": targets_intake,
            "after_converge1": targets_after_c1,
            "after_converge2": targets_after_c2,
        },
        "graph_after_c1": after_c1,
        "graph_after": synced,
        "converge1_completed": c1_ok,
        "plasticos_noop": {
            "converge_completed": pc_ok,
            "entity_id": pid,
            "accepted": pacc.get("payload"),
            "fields": pfields,
            "observed": noop,
        },
    }
    retained = any(t["state"] == "retained" for t in targets_intake)
    consumed_rows = c1_consumed_rows(targets_after_c1)
    req27 = p27_requirements(
        accepted=acc_ok,
        converge1_completed=c1_ok,
        consumed=consumed,
        retained=retained,
        consumed_row=bool(consumed_rows),
    )
    ok27 = all(req27.values())
    missing27 = [k for k, v in req27.items() if not v]
    p27 = Result(
        "P27",
        "A8",
        p,
        "accepted then consumed on a later pass, with distinct accepted/retained/consumed evidence",
        verdict(ok27, p, signed_claim=False),
        observed=obs,
        reason=(
            "accepted through Gate; retained in graph_return_targets after intake; consumed by the next "
            "converge (consumed_by converge:<op>, outcome seeded). The inference is the driver's ceg-gl "
            "rig: CEG has no Gate-routable inference trigger (F-A1-02, CEG-W)"
        )
        if ok27
        else "not observed: " + "; ".join(missing27),
    )
    resynced = after_c1 == [['"West"']]
    p30 = Result(
        "P30",
        "A8",
        p,
        "graph inference reaches a later authorized EIE decision: changed fact + resync, or "
        "a justified no-op",
        "BLOCKED",
        observed=obs,
        reason=(
            "changed fact "
            + (
                "observed in the converge answer"
                if c1_ok and consumed
                else "NOT observed (converge c1 did not complete with region=West)"
            )
            + "; resync to CEG "
            + (
                "observed (Station.region = West right after that pass)"
                if resynced
                else "NOT observed"
            )
            + "; plasticos justified no-op "
            + (
                ("observed (region undeclared, absent from fields)" if noop else "NOT observed")
                if pc_ok
                else "evidence missing (plasticos converge did not complete)"
            )
            + ". BLOCKED, not PASS: the inference is the driver's ceg-gl rig, because CEG has no "
            "Gate-routable inference trigger (F-A1-02, CEG-W), so the integrated CEG origin is unproven"
        ),
    )
    return [p27, p30]


P22_POSTAL_CODE = "94105"
P22_EXPECTED_REGION = "West"  # infer_geography_from_postal_code for US zip 94105
P22_SETTLE_S = 60.0
P22_WITNESS_S = 90.0  # budget for the resync to complete at Gate and CEG to evaluate it
# After the resync completed, how long a delivery CEG scheduled from it has to reach Gate.
P22_DELIVERY_GRACE_S = 15.0


def obligation(entity_id: str) -> dict:
    """CEG's durable inference obligation for one synthneutral Station (empty when none exists)."""
    rows = cypher(
        "synthneutral",
        "MATCH (o:CegInferenceObligation {tenant:'synthneutral', entity_type:'Station', "
        f"entity_id:'{entity_id}'}}) RETURN o.status, o.decision, o.evidence_revision, "
        "o.rule_revision, o.attempts, o.accepted_key, o.wire_key, o.accepted_findings_digest",
    )
    if not rows:
        return {}
    keys = (
        "status",
        "decision",
        "evidence_revision",
        "rule_revision",
        "attempts",
        "accepted_key",
        "wire_key",
        "accepted_findings_digest",
    )
    return {k: (v.strip('"') if v != "NULL" else None) for k, v in zip(keys, rows[0], strict=False)}


def return_target_citations(entity_id: str) -> list[dict]:
    """EIE's received graph-return targets for one synthneutral entity, with what they cite."""
    rows = psql(
        "select field_name, rule, value::text, producer_key, origin_packet_id, state "
        "from graph_return_targets "
        f"where tenant_id='synthneutral' and entity_id='{entity_id}' order by created_at"
    )
    keys = ("field", "rule", "value", "producer_key", "origin_packet_id", "state")
    return [dict(zip(keys, (r.split("|") + [""] * 6)[:6], strict=False)) for r in rows]


def ceg_init_schema(tenant: str) -> dict:
    """Declared operator step: CEG's own admin init_schema, run inside the CEG container.

    admin is operator-only and not advertised to Gate, so this is a deployment step, not a Gate call.
    scripts/ceg_init_schema.py calls engine.handlers.handle_admin; no constraint is restated here.
    Returns CEG's answer, the constraints present afterwards, and CEG's warnings.
    """
    script = (HERE / "scripts" / "ceg_init_schema.py").read_text()
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", "l9e2e-ceg", "python", "-", tenant],
            input=script,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeTimeoutError(f"CEG admin init_schema for {tenant} timed out after 120s") from exc
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    try:
        answer = json.loads(lines[-1]) if lines else {}
    except ValueError:
        answer = {}
    if r.returncode != 0 or answer.get("status") != "schema_initialized":
        raise ProbeError(f"CEG admin init_schema for {tenant} did not complete (rc {r.returncode})")
    present = [
        row[0].strip('"') for row in cypher(tenant, "SHOW CONSTRAINTS YIELD name RETURN name")
    ]
    warnings = [redact(ln)[-300:] for ln in r.stderr.splitlines() if "failed" in ln.lower()]
    return {"answer": answer, "constraints_present": present, "ceg_warnings": warnings}


def p22_requirements(
    *,
    changed_converge_completed: bool,
    input_synced: bool,
    first_leg_resolved: bool,
    graph_results: int,
    graph_results_completed: int,
    obligation_accepted: bool,
    revisions_cited: bool,
    eie_target_cites_ceg: bool,
    resync_through_gate: bool,
    resync_results: int,
    revisions_unchanged: bool,
) -> dict[str, bool]:
    """Every P22 requirement by name; P22 passes only when all hold."""
    return {
        "changed-fact converge completed": changed_converge_completed,
        "rule input (postal_code) synced to the Station": input_synced,
        "first converge's graph leg resolved before the resync window": first_leg_resolved,
        "exactly one graph-inference-result from graph through Gate": graph_results == 1,
        "that result completed at Gate": graph_results_completed == 1,
        "CEG obligation accepted (EIE intake ack)": obligation_accepted,
        "evidence_revision and rule_revision cited": revisions_cited,
        "EIE target cites CEG's rule and graph provenance": eie_target_cites_ceg,
        "identical resync reached CEG through Gate": resync_through_gate,
        "identical resync sent no graph-inference-result": resync_results == 0,
        "obligation revisions unchanged by the identical resync": revisions_unchanged,
    }


def graph_result_completions(events: list[dict], ingress: list[dict]) -> list[dict]:
    """Gate's completions of CEG's graph-inference-result: the answering worker is EIE.

    Gate stamps a completion with the node that answered, not the one that sent, so a result CEG sent
    completes as source_node enrichment-engine under the same request_id.
    """
    ids = {i["request_id"] for i in ingress}
    return [
        e
        for e in events
        if e["event"] == "gate.completed"
        and e["source_node"] == "enrichment-engine"
        and e["request_id"] in ids
    ]


def sync_completions(events: list[dict], exclude: frozenset[str] = frozenset()) -> list[dict]:
    """Gate's completions of EIE's syncs: answered by graph under the ingress request_id.

    ``exclude`` drops request ids already seen before this window, so an earlier sync whose events
    share the window's first second never counts as this one.
    """
    ids = {
        e["request_id"]
        for e in events
        if e["event"] == "gate.ingress"
        and e["source_node"] == "enrichment-engine"
        and e["request_id"] not in exclude
    }
    return [
        e
        for e in events
        if e["event"] == "gate.completed" and e["source_node"] == "graph" and e["request_id"] in ids
    ]


def eie_target_cites(targets: list[dict], accepted_key: str, wire_key: str | None) -> bool:
    """EIE retained exactly CEG's finding (region = West by the authorized rule) under CEG's key."""
    if not accepted_key:
        return False
    keys = {accepted_key, wire_key or accepted_key}
    for t in targets:
        try:
            value = json.loads(t["value"])
        except ValueError:
            continue
        if isinstance(value, dict):
            value = value.get("value")
        if (
            t["state"] == "retained"
            and t["rule"] == "infer_geography_from_postal_code"
            and t["field"] == "region"
            and value == P22_EXPECTED_REGION
            and t["producer_key"] in keys
        ):
            return True
    return False


def wait_leg_resolved(since: str, entity_id: str, budget_s: float) -> bool:
    """Poll until EIE logged this entity's graph leg as resolved and synced, within the budget."""
    deadline = time.monotonic() + budget_s
    while True:
        lines = logs("l9e2e-eie", since, ("side_effects_graph_leg_resolved",), limit=None)
        if True in graph_leg_outcomes(lines, entity_id):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(3.0, remaining))


def _wait_resync_evaluated(
    since: str, budget_s: float, exclude: frozenset[str] = frozenset(), entity_id: str = ""
) -> tuple[list[dict], list[str]]:
    """Poll until the resync's own sync completed at Gate, and CEG logged its evaluation.

    The binding is the completion: Gate's completion of a sync whose request_id the earlier pass did
    not use (``exclude``). CEG's sync handler awaits the trigger evaluation and logs it before it
    answers (engine/handlers.py:1625 _schedule_graph_inference), so that completion proves CEG
    evaluated this sync; the evaluation log line is recorded alongside. The caller isolates the window
    (``since`` stamped a full second after the earlier pass settled).

    With ``entity_id`` the wait also needs EIE's own graph-leg record for that entity, synced, in the
    window: EIE resolves the leg only after the sync's answer came back, so the record binds the
    completion to this entity's resync rather than to other traffic (a recovery sync, for one).
    """
    deadline = time.monotonic() + budget_s
    syncs: list[dict] = []
    evaluated: list[str] = []
    while time.monotonic() < deadline:
        syncs = gate_events(since, "sync")
        evaluated = logs("l9e2e-ceg", since, ("graph inference after sync",), limit=None)
        leg_done = not entity_id or True in graph_leg_outcomes(
            logs("l9e2e-eie", since, ("side_effects_graph_leg_resolved",), limit=None), entity_id
        )
        if sync_completions(syncs, exclude) and evaluated and leg_done:
            return syncs, evaluated
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
    return syncs, evaluated


def _wait_obligation_settled(entity_id: str, budget_s: float) -> dict:
    deadline = time.monotonic() + budget_s
    ob: dict = {}
    while time.monotonic() < deadline:
        ob = obligation(entity_id)
        if ob.get("status") not in (None, "pending"):
            return ob
        time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
    return ob


def c_graph_trigger(p: str) -> list[Result]:
    """P22: a material graph change through EIE's own sync makes CEG send one cited finding.

    CEG's graph-read trigger (A3, CEG #540, GRAPH_INFERENCE_TRIGGER_ENABLED) evaluates the synced
    Station as stored, runs the rule the synthneutral pack authorizes
    (plugins.inference_rules: infer_geography_from_postal_code) and sends one graph-inference-result
    through Gate. The converge schema names postal_code, which the deterministic provider echoes from
    the entity, so postal_code is an admitted field of EIE's sync row. An identical resync is the
    no-change witness: the revisions are unchanged and nothing is sent.
    """
    eid = f"sn-tr-{uuid.uuid4().hex[:6]}"
    sch = {"postal_code": "string", "band": "string"}
    extra = {"postal_code": P22_POSTAL_CODE}
    try:
        operator_step = ceg_init_schema("synthneutral")
        since = stamp()
        c1 = drive("execute", converge_payload(eid, "synthneutral", extra=extra, schema=sch))
        ob1 = _wait_obligation_settled(eid, P22_SETTLE_S)
        node = cypher(
            "synthneutral",
            f"MATCH (s:Station {{station_id:'{eid}'}}) RETURN s.postal_code, s.region",
        )
        results1 = gate_events(since, "graph-inference-result")
        targets = return_target_citations(eid)
        ceg_log1 = logs("l9e2e-ceg", since, ("graph inference",), limit=None)

        # Isolate the resync's window: c1 has settled (its obligation decided), its sync request ids are
        # excluded, and a full second passes so second-precision `since2` starts after c1's events.
        # c1's own graph leg must have resolved before c2's window opens: EIE runs the leg detached,
        # and a late c1 record inside c2's window would satisfy c2's entity-bound wait.
        c1_leg_done = wait_leg_resolved(since, eid, P22_WITNESS_S)
        c1_sync_ids = frozenset(
            e["request_id"] for e in gate_events(since, "sync") if e.get("request_id")
        )
        time.sleep(1.1)
        since2 = stamp()
        c2 = drive("execute", converge_payload(eid, "synthneutral", extra=extra, schema=sch))
        # EIE's graph leg runs detached, so the converge can answer before its sync reaches CEG.
        # The no-result witness is read only after the resync completed at Gate and CEG logged its
        # evaluation; a budget that runs out leaves the witness unproven, never vacuously true.
        syncs2, ceg_log2 = _wait_resync_evaluated(since2, P22_WITNESS_S, c1_sync_ids, eid)
        resync_leg = graph_leg_outcomes(
            logs("l9e2e-eie", since2, ("side_effects_graph_leg_resolved",), limit=None), eid
        )
        # A delivery is scheduled only after CEG answered the sync; give it a bounded window to show.
        if sync_completions(syncs2, c1_sync_ids):
            time.sleep(P22_DELIVERY_GRACE_S)
        ob2 = obligation(eid)
        results2 = gate_events(since2, "graph-inference-result")
    except ProbeError as exc:
        return [not_run("P22", "A8", p, "graph-read inference trigger", str(exc), entity_id=eid)]

    def _completed(r: dict) -> bool:
        return r.get("ok") is True and (r.get("payload") or {}).get("state") == "completed"

    ingress = [e for e in results1 if e["event"] == "gate.ingress" and e["source_node"] == "graph"]
    completed = graph_result_completions(results1, ingress)
    resync_done = sync_completions(syncs2, c1_sync_ids)
    ev_rev, rule_rev = ob1.get("evidence_revision") or "", ob1.get("rule_revision") or ""
    # CEG's accepted key (obligation_key) binds tenant, entity, both revisions and the finding set;
    # EIE records the packet's idempotency key as producer_key, so equal keys join the two records.
    accepted_key = ob1.get("accepted_key") or ""
    cited = bool(ev_rev and rule_rev and accepted_key)
    eie_cites = eie_target_cites(targets, accepted_key, ob1.get("wire_key"))
    req = p22_requirements(
        changed_converge_completed=_completed(c1),
        input_synced=bool(node) and node[0][0] == f'"{P22_POSTAL_CODE}"',
        first_leg_resolved=c1_leg_done,
        graph_results=len(ingress),
        graph_results_completed=len(completed),
        obligation_accepted=ob1.get("status") == "accepted",
        revisions_cited=cited,
        eie_target_cites_ceg=eie_cites,
        resync_through_gate=_completed(c2)
        and bool(resync_done)
        and bool(ceg_log2)
        and True in resync_leg,
        resync_results=len(
            [e for e in results2 if e["source_node"] == "graph" and e["event"] == "gate.ingress"]
        ),
        revisions_unchanged=bool(ob2)
        and (ob2.get("evidence_revision"), ob2.get("rule_revision")) == (ev_rev, rule_rev),
    )
    ok = all(req.values())
    obs = {
        "entity_id": eid,
        "operator_step": {"ceg_admin_init_schema": operator_step},
        "changed_fact": {
            "converge": {k: c1.get(k) for k in ("ok", "elapsed_s", "error")},
            "state": (c1.get("payload") or {}).get("state"),
            "station": node,
            "gate_graph_inference_results": results1,
            "obligation": ob1,
            "eie_return_targets": targets,
            "ceg_log": ceg_log1[-20:],
        },
        "no_change_witness": {
            "converge": {k: c2.get(k) for k in ("ok", "elapsed_s", "error")},
            "state": (c2.get("payload") or {}).get("state"),
            "gate_sync_events": syncs2,
            "gate_graph_inference_results": results2,
            "obligation": ob2,
            "ceg_log": ceg_log2[-20:],
        },
        "requirements": req,
    }
    return [
        Result(
            "P22",
            "A8",
            p,
            "material graph change with an authorized rule: CEG uses graph-read evidence and sends "
            "one cited finding; an identical resync sends nothing",
            verdict(ok, p, signed_claim=True),
            observed=obs,
            reason=(
                "CEG's graph-read trigger sent exactly one graph-inference-result through Gate, citing "
                "evidence_revision and rule_revision, accepted by EIE; the identical resync sent none. "
                "Rig: the obligation constraint is installed from CEG's own statement because "
                "init_schema is reachable only through CEG admin, which CEG does not advertise to Gate"
            )
            if ok
            else "not observed: " + "; ".join(k for k, v in req.items() if not v),
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
    "mixed_alg": c_mixed_alg,
    "registration": c_registration,
    "gate_rail_policy": c_gate_rail_policy,
    "feedback": c_feedback,
    "graph_trigger": c_graph_trigger,
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
    "mixed_alg": ("MIX-ALG-01",),
    "registration": ("F-A1-05", "P11-owner-collision"),
    "gate_rail_policy": ("F-A1-11",),
    "feedback": ("P27", "P30"),
    "graph_trigger": ("P22",),
}


SDK_DIGEST_LINE = re.compile(r"\S+ \d+ [0-9a-f]{64}")
# The release the integrated rerun is judged at: Gate_SDK v1.3.4 (tag v1.3.4 = v1 = 5c1cfc20), as
# "<version> <file count> <code digest>" computed by sdk_digests() over src/constellation_node_sdk at
# that tag. GL_EXPECT_SDK names another release explicitly; it cannot be empty.
EXPECTED_SDK = "1.3.4 51 08b787178555e68532ea3f71500eb1e56c2ff03672febd2230a9e4f4add6f95e"


def sdk_pin(digests: dict, expected: str) -> dict:
    """Whether every process runs exactly the expected SDK code. Unavailable or different is a mismatch."""
    if not SDK_DIGEST_LINE.fullmatch(expected):
        msg = f"expected SDK line is not '<version> <files> <sha256>': {expected!r}"
        raise ValueError(msg)
    mismatched = {c: v for c, v in digests.items() if v != expected}
    return {
        "expected": expected,
        "match": bool(digests) and not mismatched,
        "mismatched": mismatched,
    }


def sdk_digests() -> dict:
    """Code digest per process: sha256 over (relative path, file sha256) of every installed SDK .py file, sorted.

    Install metadata (RECORD, direct_url) is excluded, so identical code gives the same digest.
    """
    probe = "\n".join(
        [
            "import hashlib, importlib.metadata as m, pathlib, sys, constellation_node_sdk as s",
            "root = pathlib.Path(s.__file__).parent",
            "fs = sorted(root.rglob('*.py'))",
            "h = hashlib.sha256()",
            "for f in fs:",
            "    h.update((str(f.relative_to(root)) + ':' + hashlib.sha256(f.read_bytes()).hexdigest() + '\\n').encode())",
            "sys.stdout.write(f\"{m.version('constellation-node-sdk')} {len(fs)} {h.hexdigest()}\\n\")",
        ]
    )
    out = {}
    for c in ("l9e2e-gate", "l9e2e-eie", "l9e2e-ceg"):
        r = sh(["docker", "exec", c, "python", "-c", probe], timeout=60)
        line = r.stdout.strip()
        # Only a successful probe printing "<version> <file count> <sha256>" is a digest.
        if r.returncode == 0 and SDK_DIGEST_LINE.fullmatch(line):
            out[c] = line
        else:
            out[c] = f"UNAVAILABLE rc={r.returncode}"
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
    # Pack digests as the running processes see them: hashed inside each container at its mount.
    packs = {}
    for c, mount, prefix in (
        ("l9e2e-ceg", "/gl/ceg_packs", ""),
        ("l9e2e-eie", "/gl/eie_domains", "eie:"),
    ):
        hr = sh(["docker", "exec", c, "sh", "-c", f"sha256sum {mount}/*/spec.yaml"], timeout=30)
        if hr.returncode != 0 or not hr.stdout.strip():
            raise ProbeError(f"cannot hash the packs mounted in {c} at {mount}")
        for ln in hr.stdout.strip().splitlines():
            digest, path = ln.split(maxsplit=1)
            packs[prefix + Path(path).parent.name] = digest
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


def run_cases(names: list[str], profile: str, ev: Path) -> tuple[list[Result], str | None]:
    """Run the named cases in order. A harness failure is NOT_RUN, never PASS.

    StackUnusableError stops the run: that case and every case after it are recorded NOT_RUN, and the
    reason is returned so main() exits non-zero. Raw exception text is never recorded.
    """
    results: list[Result] = []
    aborted: str | None = None
    for name in names:
        if aborted is not None:
            res = [
                not_run(cid, "A8", profile, "harness", f"not run: {aborted}")
                for cid in CASE_IDS.get(name, (name,))
            ]
        else:
            try:
                res = CASES[name](profile)
            except Exception as exc:
                why = (
                    f"ProbeError: {redact(str(exc))[:200]}"
                    if isinstance(exc, ProbeError)
                    else type(exc).__name__
                )
                if isinstance(exc, StackUnusableError):
                    aborted = f"stack unusable after case {name!r}: {redact(str(exc))[:200]}"
                res = [
                    not_run(cid, "A8", profile, "harness", why)
                    for cid in CASE_IDS.get(name, (name,))
                ]
        for r in res:
            results.append(r)
            sys.stdout.write(f"{r.id:5} {r.verdict:14} {r.reason[:100]}\n")
            sys.stdout.flush()
            write_json(ev / f"{r.id}.json", asdict(r))
    return results, aborted


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
            sys.stdout.write(json.dumps({"evidence": str(ev), "boot": "FAILED"}) + "\n")
            return 4
    OBSERVED_PROFILE.update(running_signing())
    write_json(ev / "ready.json", wait_ready())
    rs = release_set(a.profile)
    rs["sdk_pin"] = sdk_pin(rs["sdk_code_digest"], os.environ.get("GL_EXPECT_SDK", EXPECTED_SDK))
    RELEASE_SET.update(rs)
    write_json(ev / "release_set.json", rs)
    aborted: str | None
    if not rs["sdk_pin"]["match"]:
        # Verdicts at another SDK are not evidence for this rerun: no case runs.
        aborted = f"SDK pin mismatch: {rs['sdk_pin']['mismatched']}"
        results = [
            not_run(cid, "A8", a.profile, "harness", aborted)
            for n in a.cases.split(",")
            for cid in CASE_IDS.get(n, (n,))
        ]
        for r in results:
            write_json(ev / f"{r.id}.json", asdict(r))
    else:
        results, aborted = run_cases(a.cases.split(","), a.profile, ev)
    leaks = [f.name for f in ev.glob("*.json") for s in secrets_() if s in f.read_text()]
    summary = {
        "stamp": run_stamp,
        "profile": a.profile,
        "observed_signing_algorithm": dict(OBSERVED_PROFILE),
        "release_set": rs,
        "leak_check": leaks or "clean",
        "aborted": aborted,
        "results": [{"id": r.id, "verdict": r.verdict, "reason": r.reason} for r in results],
    }
    write_json(ev / "summary.json", summary)
    sys.stdout.write(
        json.dumps({"evidence": str(ev), "leak_check": summary["leak_check"], "aborted": aborted})
        + "\n"
    )
    if leaks:
        return 3
    return 5 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
