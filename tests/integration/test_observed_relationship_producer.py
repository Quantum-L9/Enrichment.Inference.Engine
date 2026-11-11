"""
tests/integration/test_observed_relationship_producer.py

QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003 + QM.POC0.TENANT_ASSOCIATION_MAPPING.004
— SYNTHETIC end-to-end witness.

Real processes, real sockets, nothing mocked between the producer and the score:

    synthetic HubSpot HTTP server -> EIE produce_observed_relationship (EIE's own
    interpreter) -> PacketRouter.notify_relationship_sync -> Gate (signed) ->
    CEG sync (RELATIONSHIP_SYNC_ENABLED=true) -> Neo4j -> CEG GraphEvidenceBinding +
    local handle_score (CEG's own interpreter)

What is synthetic, and labelled so everywhere: the HubSpot answer (a local
``http.server`` returning the documented
``GET /crm/objects/2026-09/{from}/{id}/associations/{to}`` shape), the record ids,
and the two disposable tenants (CEG's own synthetic verification domains,
``tests/fixtures/domains``, extended with a synthetic ``observedfrom`` mapping,
node ``refnamespace`` declarations and a reviewed ``evidencerelevance``). This is
not live CRM evidence and none of it is a real tenant's configuration.

What is real: the Gate, EIE and CEG processes and their signed TransportPackets,
the Neo4j database, CEG's spec validation and write path, and CEG's scorer with
relevance loaded from the tenant spec. The operator passes only an
``ObservedAssociationKey`` and two record ids; the relationship, direction and
node refs come from the tenant spec. CEG reads ``domains_root``; EIE reads its
own lockstep mirror copy (``eie_domains_root``), as in production.

The processes come from Constellation.Gate's seam harness
(``constellation-gate/tests/e2e/seam/harness.py``), imported from the sibling
checkout. The test is skipped when that layout, a CEG checkout with a ``.venv``,
or a disposable Neo4j (``L9_SEAM_NEO4J_URI`` / ``L9_SEAM_NEO4J_PASSWORD``) is not
available: the same environment condition the harness itself skips on. Under
``EIE_REQUIRE_INTEGRATION=1`` (CI's ``integration`` job) each of those skips is a
failure instead.
Integration fixtures run ``DETACH DELETE`` on the two tenant databases they
create; point them only at a disposable instance.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import yaml

from app.engines.observed_association_contract import (
    MAPPING_UNVERIFIED_MIRROR,
    mapping_fingerprint,
    mirror_drift,
)
from tests.conftest import skip_or_fail_unavailable

pytestmark = pytest.mark.integration

EIE_ROOT = Path(__file__).resolve().parents[2]
_SIBLINGS = EIE_ROOT.parent


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


GATE_ROOT = _path_env("L9_SEAM_GATE_ROOT", _SIBLINGS / "Constellation.Gate" / "constellation-gate")
CEG_ROOT = _path_env("L9_SEAM_CEG_ROOT", _SIBLINGS / "Cognitive.Engine.Graphs")
NEO4J_PASSWORD = os.environ.get("L9_SEAM_NEO4J_PASSWORD", "")

# CEG's synthetic verification domains, copied under disposable tenant ids with
# one relationship sync endpoint added for an edge their ontology already
# declares. Record ids are HubSpot-shaped (numeric) and fabricated.
VOCABULARIES: dict[str, dict[str, Any]] = {
    "it-observed-cave": {
        "source": "cheese-aging",
        "extra_endpoints": [
            {"path": "/v1/sync/affineurs", "targetnode": "Affineur", "idproperty": "affineur_id"},
            {
                "path": "/v1/sync/tended-by",
                "targetedge": "TENDED_BY",
                "fromnode": "AgingCave",
                "tonode": "Affineur",
            },
        ],
        "subject": ("aging-caves", "AgingCave", "cave_id"),
        "object": ("affineurs", "Affineur", "affineur_id"),
        "edge": ("tended-by", "TENDED_BY"),
        "provider": ("companies", "deals", "HUBSPOT_DEFINED", 342),
        "subjects": ["71000001", "71000002"],
        "target": "81000009",
        "ref_namespaces": ("it.cave", "it.affineur"),
        "node_endpoints": ("/v1/sync/aging-caves", "/v1/sync/affineurs"),
        "relevance": 0.5,
    },
    "it-observed-orbit": {
        "source": "orbital-logistics",
        "extra_endpoints": [
            {
                "path": "/v1/sync/stocks",
                "targetedge": "STOCKS",
                "fromnode": "OrbitalDepot",
                "tonode": "PropellantGrade",
            },
        ],
        "subject": ("orbital-depots", "OrbitalDepot", "depot_id"),
        "object": ("propellant-grades", "PropellantGrade", "grade_code"),
        "edge": ("stocks", "STOCKS"),
        "provider": ("deals", "contacts", "USER_DEFINED", 17),
        "subjects": ["72000001", "72000002"],
        "target": "82000009",
        "ref_namespaces": ("it.depot", "it.grade"),
        "node_endpoints": ("/v1/sync/orbital-depots", "/v1/sync/propellant-grades"),
        "relevance": 1.0,
    },
}
CAVE, ORBIT = "it-observed-cave", "it-observed-orbit"


def subject_refs(tenant: str) -> list[str]:
    """Graph stable ids of the subject nodes: ``<namespace>:<HubSpot record id>``."""
    v = VOCABULARIES[tenant]
    return [f"{v['ref_namespaces'][0]}:{record}" for record in v["subjects"]]


def object_ref(tenant: str) -> str:
    v = VOCABULARIES[tenant]
    return f"{v['ref_namespaces'][1]}:{v['target']}"


EIE_DRIVER = r"""
import asyncio, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
args = json.loads(os.environ["SEAM_ARGS"])

def out(obj):
    print(json.dumps(obj, default=str))

async def main():
    from app.core.config import get_settings
    from app.engines.packet_router import get_router
    router = get_router(get_settings())
    if args["op"] == "seed":
        results = []
        for node in args["nodes"]:
            ack = await router.notify_graph_sync(
                args["tenant"], node["id"], {node["id_property"]: node["id"]}, node_label=node["label"]
            )
            results.append(ack is not None)
        out({"ok": all(results), "results": results})
        return
    # op == "produce": a SYNTHETIC HubSpot answers on a real local socket.
    seen = []
    status, body = args["hubspot_status"], args["hubspot_body"]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path, "auth": self.headers.get("Authorization", "").split(" ")[0]})
            payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    from app.services.crm.base import CRMCredentials, CRMType
    from app.services.crm.hubspot_client import HubSpotClient
    from app.services.crm.observed_relationship_producer import (
        ObservedAssociationKey, produce_observed_relationship,
    )
    client = HubSpotClient(CRMCredentials(crm_type=CRMType.HUBSPOT, credentials={"access_token": "synthetic-e2e"}))
    client._base_url = f"http://127.0.0.1:{server.server_address[1]}"  # synthetic source only
    result = await produce_observed_relationship(
        tenant_id=args["tenant"],
        key=ObservedAssociationKey(**args["key"]),
        from_record_id=args["from_id"],
        to_record_id=args["to_id"],
        hubspot=client,
        router=router,
    )
    server.shutdown()
    out({"ok": True, "status": str(result.status), "detail": result.detail,
         "evidence_ref": result.evidence_ref, "source_ref": result.source_ref,
         "ceg_ack": result.ceg_ack, "hubspot_requests": seen,
         "mapping_fingerprint": result.mapping_fingerprint,
         "mapping_verification": result.mapping_verification})

asyncio.run(main())
"""

CEG_DRIVER = r"""
import asyncio, json, os
args = json.loads(os.environ["SEAM_ARGS"])

def out(obj):
    print(json.dumps(obj, default=str))

async def main():
    from engine.graph.driver import GraphDriver
    driver = GraphDriver(os.environ["NEO4J_URI"], os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
    await driver.connect()
    try:
        if args["op"] == "cypher":
            rows = await driver.execute_query(args["cypher"], args.get("parameters", {}), database=args["database"])
            out({"ok": True, "rows": rows})
        elif args["op"] == "score":
            from engine.config.loader import DomainPackLoader
            from engine.handlers import handle_score
            from engine.scoring.graph_evidence import GraphEvidenceBinding, approved_relevance_from_spec
            # Relevance is the tenant spec's own reviewed evidencerelevance (D-8/D-8b).
            spec = DomainPackLoader(config_path=os.environ["DOMAINS_ROOT"]).load_domain(args["tenant"])
            binding = GraphEvidenceBinding(driver, approved_relevance_from_spec(spec))
            result = await handle_score(args["tenant"], {"entity_refs": args["refs"], "top_n": len(args["refs"])}, binding=binding)
            out({"ok": True, "result": result})
    finally:
        await driver.close()

asyncio.run(main())
"""


def _load_harness() -> ModuleType | None:
    path = GATE_ROOT / "tests" / "e2e" / "seam" / "harness.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("constellation_seam_harness", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _gate_python() -> Path | None:
    raw = os.environ.get("L9_SEAM_GATE_PYTHON", "").strip()
    candidates = [Path(raw)] if raw else []
    candidates += [
        GATE_ROOT / ".venv" / "bin" / "python",
        GATE_ROOT.parent / ".venv" / "bin" / "python",
    ]
    return next((c for c in candidates if c.exists()), None)


# ── Session: disposable tenants, three real processes ─────────────────────────


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    module = _load_harness()
    gate_python = _gate_python()
    if (
        module is None
        or gate_python is None
        or not (CEG_ROOT / ".venv" / "bin" / "python").exists()
    ):
        skip_or_fail_unavailable(
            "synthetic E2E needs sibling Constellation.Gate (seam harness + .venv) and "
            "Cognitive.Engine.Graphs (.venv) checkouts, or L9_SEAM_GATE_ROOT / L9_SEAM_CEG_ROOT"
        )
    if not NEO4J_PASSWORD:
        skip_or_fail_unavailable(
            "synthetic E2E needs a disposable Neo4j: set L9_SEAM_NEO4J_URI / L9_SEAM_NEO4J_PASSWORD"
        )
    return module


@pytest.fixture(scope="module")
def domains_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("observed-domains")
    for tenant, vocabulary in VOCABULARIES.items():
        source_dir = CEG_ROOT / "tests" / "fixtures" / "domains" / vocabulary["source"]
        spec = yaml.safe_load((source_dir / "spec.yaml").read_text(encoding="utf-8"))
        spec["domain"]["id"] = tenant
        # SYNTHETIC approval: a reviewed spec, so CEG honours its relevance (D-8b).
        spec["domain"]["version"] = "1.0.0-reviewed"
        for endpoint in vocabulary["extra_endpoints"]:
            spec["sync"]["endpoints"].append(
                {"method": "POST", "batchstrategy": "UNWINDMERGE", **endpoint}
            )
        from_type, to_type, category, type_id = vocabulary["provider"]
        for endpoint in spec["sync"]["endpoints"]:
            if endpoint["path"] in vocabulary["node_endpoints"]:
                index = vocabulary["node_endpoints"].index(endpoint["path"])
                endpoint["refnamespace"] = vocabulary["ref_namespaces"][index]
            if endpoint.get("targetedge") == vocabulary["edge"][1]:
                endpoint["observedfrom"] = [
                    {
                        "provider": "hubspot",
                        "fromobjecttype": from_type,
                        "toobjecttype": to_type,
                        "associationcategory": category,
                        "associationtypeid": type_id,
                    }
                ]
        for edge in spec["ontology"]["edges"]:
            if edge["type"] == vocabulary["edge"][1]:
                edge["evidencerelevance"] = vocabulary["relevance"]
        (root / tenant).mkdir()
        (root / tenant / "spec.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=False), encoding="utf-8"
        )
        for extra in source_dir.iterdir():
            if extra.name != "spec.yaml" and extra.is_file():
                shutil.copy(extra, root / tenant / extra.name)
    return root


@pytest.fixture(scope="module")
def eie_domains_root(domains_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """EIE's own lockstep mirror of the tenant specs CEG serves (a copy, not the same file)."""
    mirror = tmp_path_factory.mktemp("observed-domains-eie-mirror")
    shutil.copytree(domains_root, mirror, dirs_exist_ok=True)
    return mirror


class Seam:
    """The live constellation plus the two driver entry points."""

    def __init__(
        self, harness: ModuleType, domains_root: Path, eie_domains_root: Path, receipt_dir: Path
    ) -> None:
        os.environ.setdefault("L9_SEAM_BASE_PORT", "19400")
        ctx = harness.build_context(receipt_dir)
        if ctx is None:
            skip_or_fail_unavailable("seam harness could not resolve EIE/CEG peers")
        ctx.gate_python = _gate_python()
        ctx.eie = harness.Peer("eie", EIE_ROOT, EIE_ROOT / ".venv" / "bin" / "python", ctx.eie.port)
        ctx.ceg = harness.Peer("ceg", CEG_ROOT, CEG_ROOT / ".venv" / "bin" / "python", ctx.ceg.port)
        self.h, self.ctx, self.domains_root = harness, ctx, domains_root
        self.eie_domains_root = eie_domains_root
        self.spec_path = receipt_dir / "ceg_spec.e2e.yaml"
        original = (CEG_ROOT / "engine" / "spec.yaml").read_text(encoding="utf-8")
        self.spec_path.write_text(
            "\n".join(
                f'  internal_url: "{ctx.ceg.url}"'
                if line.strip().startswith("internal_url:")
                else line
                for line in original.splitlines()
            )
            + "\n",
            encoding="utf-8",
        )
        self.gate = self._process(
            "constellation-gate",
            [
                str(ctx.gate_python),
                "-m",
                "uvicorn",
                "constellation_gate.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(ctx.gate_port),
            ],
            ctx.gate_root,
            ctx.gate_env(),
            "gate.log",
        )
        self.ceg = self._process(
            "cognitive-engine-graphs",
            [
                str(ctx.ceg.python),
                "-m",
                "uvicorn",
                "chassis.entrypoint:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(ctx.ceg.port),
            ],
            ctx.ceg.root,
            self.ceg_env(),
            "ceg.log",
        )
        self.eie = self._process(
            "enrichment-inference-engine",
            [
                str(ctx.eie.python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(ctx.eie.port),
            ],
            ctx.eie.root,
            self.eie_env(),
            "eie.log",
        )

    def _process(self, label: str, cmd: list[str], cwd: Path, env: dict[str, str], log: str) -> Any:
        return self.h.ManagedProcess(
            label=label, cmd=cmd, cwd=cwd, env=env, log_path=self.ctx.receipt_dir / log
        )

    def eie_env(self) -> dict[str, str]:
        return {**self.ctx.eie_env(), "DOMAINS_DIR": str(self.eie_domains_root)}

    def ceg_env(self) -> dict[str, str]:
        return {
            **self.ctx.ceg_env(self.spec_path),
            "DOMAINS_ROOT": str(self.domains_root),
            # Disposable environment only: the flag ships False.
            "RELATIONSHIP_SYNC_ENABLED": "true",
        }

    def start(self, *, gate: bool = True, ceg: bool = True, eie: bool = True) -> None:
        if gate:
            self.gate.start()
            self.gate.wait_http(f"{self.ctx.gate_url}/v1/health")
        if ceg:
            self.ceg.start()
            self.ceg.wait_http(f"{self.ctx.ceg.url}/v1/health", ok=(200, 503))
        if eie:
            self.eie.start()
            self.eie.wait_http(f"{self.ctx.eie.url}/api/v1/health", ok=(200, 503))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            registry = httpx.get(
                f"{self.ctx.gate_url}/v1/registry",
                headers={"X-Admin-Token": self.ctx.admin_token},
                timeout=5.0,
            ).json()
            if "enrichment-engine" in registry and "graph" in registry:
                return
            time.sleep(1.0)
        pytest.fail(f"nodes did not register with Gate: {list(registry)}")

    def stop(self) -> None:
        for proc in (self.eie, self.ceg, self.gate):
            proc.stop()

    def gate_executes(self) -> int:
        return self.h.count_matching(self.gate.log_text(), "/v1/execute")

    def eie_call(self, **args: Any) -> dict[str, Any]:
        return self.h.run_in_peer(
            self.ctx.eie, EIE_DRIVER, {**self.eie_env(), "SEAM_ARGS": json.dumps(args)}
        )

    def ceg_call(self, **args: Any) -> dict[str, Any]:
        return self.h.run_in_peer(
            self.ctx.ceg, CEG_DRIVER, {**self.ceg_env(), "SEAM_ARGS": json.dumps(args)}
        )

    # ── convenience ──

    def produce(
        self,
        tenant: str,
        *,
        key_overrides: dict[str, Any] | None = None,
        hubspot_status: int = 200,
        hubspot_body: Any = None,
        from_id: str | None = None,
        to_id: str | None = None,
        vocabulary: str | None = None,
    ) -> dict[str, Any]:
        v = VOCABULARIES[vocabulary or tenant]
        from_type, to_type, category, type_id = v["provider"]
        # The operator names only the provider association; the tenant spec decides the rest.
        key = {
            "from_object_type": from_type,
            "to_object_type": to_type,
            "association_category": category,
            "association_type_id": type_id,
            **(key_overrides or {}),
        }
        body = hubspot_body if hubspot_body is not None else association_body(vocabulary or tenant)
        return self.eie_call(
            op="produce",
            tenant=tenant,
            key=key,
            hubspot_status=hubspot_status,
            hubspot_body=body,
            from_id=from_id or v["subjects"][0],
            to_id=to_id or v["target"],
        )

    def edges(self, tenant: str) -> list[dict[str, Any]]:
        return self.ceg_call(
            op="cypher",
            database=tenant,
            cypher=(
                "MATCH (a)-[r]->(b) RETURN labels(a) AS from_labels, type(r) AS type, labels(b) AS to_labels, "
                "properties(r) AS props ORDER BY props.evidence_ref"
            ),
        )["rows"]

    def score(self, tenant: str, refs: list[str] | None = None) -> dict[str, Any]:
        return self.ceg_call(op="score", tenant=tenant, refs=refs or subject_refs(tenant))["result"]


def association_body(
    tenant: str, *, to_id: str | None = None, type_id: int | None = None
) -> dict[str, Any]:
    """SYNTHETIC HubSpot association answer: one bound type plus an unrelated label."""
    v = VOCABULARIES[tenant]
    _from, _to, category, bound_type = v["provider"]
    return {
        "results": [
            {
                "toObjectId": int(to_id or v["target"]),
                "associationTypes": [
                    {
                        "category": category,
                        "typeId": type_id if type_id is not None else bound_type,
                        "label": None,
                    },
                    {
                        "category": "USER_DEFINED",
                        "typeId": 9001,
                        "label": "Synthetic unrelated label",
                    },
                ],
            }
        ]
    }


@pytest.fixture(scope="module")
def seam(
    harness: ModuleType,
    domains_root: Path,
    eie_domains_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Seam]:
    live = Seam(
        harness, domains_root, eie_domains_root, tmp_path_factory.mktemp("observed-e2e-receipts")
    )
    for tenant in VOCABULARIES:
        live.ceg_call(
            op="cypher", database="system", cypher=f"CREATE DATABASE `{tenant}` IF NOT EXISTS WAIT"
        )
        live.ceg_call(op="cypher", database=tenant, cypher="MATCH (n) DETACH DELETE n")
    live.start()
    for tenant, v in VOCABULARIES.items():
        (_, subject_label, subject_id), (_, object_label, object_id) = v["subject"], v["object"]
        nodes = [
            {"id": ref, "label": subject_label, "id_property": subject_id}
            for ref in subject_refs(tenant)
        ]
        nodes.append({"id": object_ref(tenant), "label": object_label, "id_property": object_id})
        seeded = live.eie_call(op="seed", tenant=tenant, nodes=nodes)
        assert seeded["ok"], f"node sync failed for {tenant}: {seeded}"
    try:
        yield live
    finally:
        live.stop()
        for tenant in VOCABULARIES:
            live.ceg_call(op="cypher", database=tenant, cypher="MATCH (n) DETACH DELETE n")


def _spec(root: Path, tenant: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (root / tenant / "spec.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _by_ref(score: dict[str, Any]) -> dict[str, Any]:
    scored = {s["entity_ref"]: s for s in score["scored"]}
    abstained = {a["entity_ref"]: a for a in score["abstained"]}
    return {"scored": scored, "abstained": abstained}


# ── The witness (ordered: each step builds on the graph the previous one left) ─


def test_e2e_observed_association_becomes_one_scored_evidence_edge(seam: Seam) -> None:
    v = VOCABULARIES[CAVE]
    produced = seam.produce(CAVE)
    assert produced["status"] == "SYNCED", produced
    assert produced["ceg_ack"]["created_count"] == 1
    from_type, to_type, category, type_id = v["provider"]
    [request] = produced["hubspot_requests"]
    assert request == {
        "path": f"/crm/objects/2026-09/{from_type}/{v['subjects'][0]}/associations/{to_type}",
        "auth": "Bearer",
    }

    [edge] = seam.edges(CAVE)
    assert edge["from_labels"] == [v["subject"][1]]
    assert edge["type"] == v["edge"][1]
    assert edge["to_labels"] == [v["object"][1]]
    props = edge["props"]
    assert props["evidence_ref"] == produced["evidence_ref"]
    assert props["source_ref"] == (
        f"hubspot:crm/associations/{from_type}/{v['subjects'][0]}->{to_type}/{v['target']}#{category}:{type_id}"
    )
    assert (props["confidence"], props["assertion_status"], props["polarity"]) == (
        1.0,
        "observed",
        "positive",
    )
    assert props["_tenant"] == CAVE
    assert props["_subject_ref"] == subject_refs(CAVE)[0]
    assert props["_object_ref"] == object_ref(CAVE)
    assert "source_revision" not in props  # HubSpot reports none; none is invented

    # The mapping came from EIE's mirror, labelled as not yet cross-checked by CEG (D-9).
    assert produced["mapping_verification"] == MAPPING_UNVERIFIED_MIRROR
    mirror = _spec(seam.eie_domains_root, CAVE)
    authority = _spec(seam.domains_root, CAVE)
    assert mirror_drift(mirror, authority) == []
    assert produced["mapping_fingerprint"] == mapping_fingerprint(authority)

    score = _by_ref(seam.score(CAVE))
    assert subject_refs(CAVE)[0] in score["scored"], score
    assert subject_refs(CAVE)[1] in score["abstained"], score  # identical flat node, no evidence
    # Observation certainty (confidence 1.0) and the spec's reviewed relevance (0.5) stay
    # separate inputs: CEG's score is their product, x100.
    assert score["scored"][subject_refs(CAVE)[0]]["score"] == 100.0 * v["relevance"]


def test_e2e_repeated_delivery_neither_duplicates_nor_changes_the_score(seam: Seam) -> None:
    before = seam.score(CAVE)
    first, again = seam.produce(CAVE), seam.produce(CAVE)
    for delivery in (first, again):
        assert delivery["status"] == "SYNCED", delivery
    # The same assertion carries the same Gate idempotency key, so Gate answers it
    # from its keyed cache with the original acknowledgment.
    assert again["ceg_ack"] == first["ceg_ack"]
    assert again["evidence_ref"] == first["evidence_ref"]
    assert len(seam.edges(CAVE)) == 1
    assert seam.score(CAVE)["scored"] == before["scored"]


def test_e2e_replay_after_full_restart_is_answered_by_ceg_identity(seam: Seam) -> None:
    first = seam.produce(CAVE)
    # Restart all three processes: a fresh Gate has neither its keyed cache nor any
    # registration, and nodes register once at boot, so every node restarts.
    seam.stop()
    seam.start()
    replay = seam.produce(CAVE)
    assert replay["status"] == "SYNCED", replay
    assert replay["evidence_ref"] == first["evidence_ref"]
    assert (replay["ceg_ack"]["created_count"], replay["ceg_ack"]["replayed_count"]) == (0, 1)
    assert len(seam.edges(CAVE)) == 1


@pytest.mark.parametrize(
    ("case", "kwargs", "status"),
    [
        (
            "provider-403",
            {"hubspot_status": 403, "hubspot_body": {"message": "missing scopes"}},
            "PROVIDER_UNAVAILABLE",
        ),
        (
            "provider-429",
            {"hubspot_status": 429, "hubspot_body": {"message": "rate limited"}},
            "PROVIDER_UNAVAILABLE",
        ),
        ("malformed-body", {"hubspot_body": "{not json"}, "PROVIDER_UNAVAILABLE"),
        (
            "unbound-type",
            {"hubspot_body": association_body(CAVE, type_id=9999)},
            "SOURCE_NOT_AVAILABLE",
        ),
        ("no-association", {"hubspot_body": {"results": []}}, "SOURCE_NOT_AVAILABLE"),
        (
            "reversed-object-types",
            {"key_overrides": {"from_object_type": "deals", "to_object_type": "companies"}},
            "CONTRACT_NOT_DECLARED",
        ),
        (
            "undeclared-association",
            {"key_overrides": {"association_type_id": 343}},
            "CONTRACT_NOT_DECLARED",
        ),
        ("foreign-tenant-key", {"vocabulary": ORBIT}, "CONTRACT_NOT_DECLARED"),
    ],
)
def test_e2e_refusals_never_reach_gate_or_the_graph(
    seam: Seam, case: str, kwargs: dict[str, Any], status: str
) -> None:
    edges_before, executes_before = seam.edges(CAVE), seam.gate_executes()
    refused = seam.produce(CAVE, **kwargs)
    assert refused["status"] == status, (case, refused)
    assert refused["ceg_ack"] is None
    if status == "CONTRACT_NOT_DECLARED":
        assert refused["hubspot_requests"] == [], f"{case} read the provider before resolving"
    assert seam.gate_executes() == executes_before, f"{case} reached Gate"
    assert seam.edges(CAVE) == edges_before


def test_e2e_missing_endpoint_node_is_refused_by_ceg_and_writes_nothing(seam: Seam) -> None:
    missing = "81000099"
    refused = seam.produce(CAVE, to_id=missing, hubspot_body=association_body(CAVE, to_id=missing))
    assert refused["status"] == "NOT_ACKNOWLEDGED", refused
    assert [e["props"]["_object_ref"] for e in seam.edges(CAVE)] == [object_ref(CAVE)]


def test_e2e_tenant_isolation(seam: Seam) -> None:
    cave = VOCABULARIES[CAVE]
    # The orbit tenant observes the cave tenant's record ids under its own mapping: its
    # spec maps them into the orbit namespaces, where no such node exists in the orbit
    # database, so CEG refuses and nothing is written in either tenant.
    foreign = seam.produce(
        ORBIT,
        from_id=cave["subjects"][0],
        to_id=cave["target"],
        hubspot_body=association_body(ORBIT, to_id=cave["target"]),
    )
    assert foreign["status"] == "NOT_ACKNOWLEDGED", foreign
    assert seam.edges(ORBIT) == []
    # The orbit tenant's own observed association works independently ...
    own = seam.produce(ORBIT)
    assert own["status"] == "SYNCED", own
    assert own["evidence_ref"] != seam.produce(CAVE)["evidence_ref"]
    [orbit_edge] = seam.edges(ORBIT)
    assert orbit_edge["props"]["_tenant"] == ORBIT
    # ... and neither tenant's score reads the other's evidence.
    assert subject_refs(CAVE)[0] not in json.dumps(seam.score(ORBIT))
    assert len(seam.edges(CAVE)) == 1


def test_e2e_removing_the_edge_makes_the_entity_abstain(seam: Seam) -> None:
    v = VOCABULARIES[CAVE]
    assert subject_refs(CAVE)[0] in _by_ref(seam.score(CAVE))["scored"]
    seam.ceg_call(op="cypher", database=CAVE, cypher=f"MATCH ()-[r:{v['edge'][1]}]->() DELETE r")
    after = _by_ref(seam.score(CAVE))
    assert after["scored"] == {}
    assert set(after["abstained"]) == set(subject_refs(CAVE))


def test_e2e_a_node_id_outside_the_declared_namespace_is_never_created(seam: Seam) -> None:
    v = VOCABULARIES[CAVE]
    _, label, id_property = v["subject"]
    seeded = seam.eie_call(
        op="seed",
        tenant=CAVE,
        nodes=[{"id": "71000077", "label": label, "id_property": id_property}],
    )
    assert seeded["results"] == [False], seeded  # CEG refused the raw provider id
    rows = seam.ceg_call(
        op="cypher",
        database=CAVE,
        cypher=f"MATCH (n:{label}) WHERE n.{id_property} = $id RETURN count(n) AS n",
        parameters={"id": "71000077"},
    )["rows"]
    assert rows == [{"n": 0}]


def test_e2e_a_drifted_mirror_is_reported_and_never_presented_as_ceg_approval(seam: Seam) -> None:
    """Residual risk until D-9 (Cognitive.Engine.Graphs#386), witnessed, not hidden.

    EIE's mirror maps an association CEG's spec does not declare. CEG cannot yet
    tell (its row carries no provenance), so the edge is written; the producer
    labels the result as an unverified mirror and ``mirror_drift`` names the
    divergence. The edge is removed afterwards.
    """
    path = seam.eie_domains_root / CAVE / "spec.yaml"
    original = path.read_text(encoding="utf-8")
    drifted = yaml.safe_load(original)
    endpoint = next(e for e in drifted["sync"]["endpoints"] if e.get("observedfrom"))
    endpoint["observedfrom"].append({**endpoint["observedfrom"][0], "associationtypeid": 9001})
    path.write_text(yaml.safe_dump(drifted, sort_keys=False), encoding="utf-8")
    try:
        drift = mirror_drift(drifted, _spec(seam.domains_root, CAVE))
        assert any("only in EIE mirror" in line for line in drift), drift
        produced = seam.produce(
            CAVE,
            key_overrides={"association_type_id": 9001},
            hubspot_body=association_body(CAVE, type_id=9001),
        )
        assert produced["status"] == "SYNCED", produced
        assert produced["mapping_verification"] == MAPPING_UNVERIFIED_MIRROR
        assert produced["mapping_fingerprint"] != mapping_fingerprint(
            _spec(seam.domains_root, CAVE)
        )
    finally:
        path.write_text(original, encoding="utf-8")
        seam.ceg_call(
            op="cypher",
            database=CAVE,
            cypher="MATCH ()-[r]->() WHERE r.source_ref ENDS WITH $suffix DELETE r",
            parameters={"suffix": ":9001"},
        )
