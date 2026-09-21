"""EIE-212-F001 — a process-local return channel and a multi-pod deployment cannot both be true.

``tests/unit/test_worker_singleton_invariant.py`` pins one uvicorn worker per
image. That closes one split-brain dimension and leaves the other open: every
Kubernetes replica is a separate process with its own ``GraphReturnChannel``
memory, so a ``graph-inference-result`` packet delivered to pod A is invisible to
the convergence loop draining the channel in pod B, and the result is dropped as
a function of which pod the Service picked. The manifests shipped ``replicas: 2``
(kustomize base), ``replicaCount: 3`` with an HPA scaling 2..10 (Helm), and a
deploy workflow defaulting production to 3.

This module asserts the topology the code can actually support, for every
supported deployment path, so horizontal scaling cannot be re-enabled by
accident before the channel moves to shared/durable state. When it does, delete
``test_two_channel_instances_do_not_share_state`` first: if that still passes,
the topology tests below are still required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.services.graph_return_channel import (
    GraphReturnChannel,
    build_graph_inference_result_envelope,
)

ROOT = Path(__file__).resolve().parents[2]
KUSTOMIZE = ROOT / "infra" / "k8s" / "kustomize"
HELM = ROOT / "infra" / "k8s" / "helm" / "enrichment-api"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "k8s-deploy.yml"

READINESS_PATH = "/api/v1/ready"
LIVENESS_PATH = "/api/v1/health"
# A rendered resource, not a comment that mentions the kind.
HPA_KIND_RE = re.compile(r"^\s*kind:\s*HorizontalPodAutoscaler\s*$", re.MULTILINE)


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The premise: two channel instances (two processes, two pods) share nothing.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_channel_instances_do_not_share_state() -> None:
    """A producer on one instance is invisible to a consumer on another.

    Each pod holds its own ``GraphReturnChannel``; constructing two instances
    here is the in-process model of two replicas. If this ever fails, the
    channel has become shared and the single-replica pin below may be lifted.
    """
    pod_a = GraphReturnChannel()
    pod_b = GraphReturnChannel()
    packet = build_graph_inference_result_envelope(
        tenant_id="acme",
        inference_outputs=[
            {
                "entity_id": "e1",
                "field": "community_id",
                "value": 42,
                "confidence": 0.95,
                "rule": "louvain",
            }
        ],
    )

    assert await pod_a.submit(packet) == 1

    # The convergence loop on the other pod drains nothing — the result is lost.
    assert await pod_b.drain("acme", timeout=0.05) == []
    # And the same drain on the producing pod sees it, proving the split is
    # topological, not a validation failure.
    drained = await pod_a.drain("acme", timeout=0.05)
    assert [t.entity_id for t in drained] == ["e1"]


# --------------------------------------------------------------------------
# Kustomize: base + every overlay
# --------------------------------------------------------------------------


def _kustomize_overlays() -> list[Path]:
    overlays = sorted((KUSTOMIZE / "overlays").glob("*/kustomization.yaml"))
    assert overlays, "no kustomize overlays found"
    return overlays


def test_kustomize_base_runs_exactly_one_replica_with_recreate() -> None:
    deployment = _load(KUSTOMIZE / "base" / "deployment.yaml")
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 1, (
        "kustomize base declares more than one replica while GraphReturnChannel "
        "is process-local; move the channel to shared state before raising this"
    )
    # RollingUpdate surges a second pod during rollout — the same split, briefly.
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_kustomize_base_renders_no_horizontal_pod_autoscaler() -> None:
    kustomization = _load(KUSTOMIZE / "base" / "kustomization.yaml")
    assert "hpa.yaml" not in kustomization["resources"]
    assert not (KUSTOMIZE / "base" / "hpa.yaml").exists()
    for manifest in (KUSTOMIZE / "base").glob("*.yaml"):
        text = manifest.read_text(encoding="utf-8")
        assert not HPA_KIND_RE.search(text), f"{manifest.name} renders an HPA"


@pytest.mark.parametrize("overlay", _kustomize_overlays(), ids=lambda p: p.parent.name)
def test_kustomize_overlay_does_not_scale_out(overlay: Path) -> None:
    """No overlay may raise replicas above 1 or add an autoscaler."""
    kustomization = _load(overlay)
    text = overlay.read_text(encoding="utf-8")
    assert not HPA_KIND_RE.search(text), f"{overlay.parent.name} adds an HPA"

    for patch in kustomization.get("patches", []) or []:
        body = patch.get("patch", "")
        ops = yaml.safe_load(body) if isinstance(body, str) else body
        if not isinstance(ops, list):
            continue
        for op in ops:
            if op.get("path") == "/spec/replicas":
                assert op.get("value") == 1, (
                    f"{overlay.parent.name} patches replicas to {op.get('value')}"
                )


# --------------------------------------------------------------------------
# Helm: every values file, and the template that renders them
# --------------------------------------------------------------------------


def _helm_values_files() -> list[Path]:
    files = sorted(HELM.glob("values*.yaml"))
    assert files, "no Helm values files found"
    return files


@pytest.mark.parametrize("values_file", _helm_values_files(), ids=lambda p: p.name)
def test_helm_values_run_exactly_one_replica_without_autoscaling(values_file: Path) -> None:
    values = _load(values_file)
    if "replicaCount" in values:
        assert values["replicaCount"] == 1, f"{values_file.name} sets replicaCount != 1"
    autoscaling = values.get("autoscaling")
    if autoscaling is not None:
        assert autoscaling.get("enabled") is False, f"{values_file.name} enables the HPA"
        assert autoscaling.get("maxReplicas", 1) == 1, f"{values_file.name} allows scale-out"


def test_helm_base_values_pin_the_topology_explicitly() -> None:
    """values.yaml is the single source of truth; the overlays may only agree."""
    values = _load(HELM / "values.yaml")
    assert values["replicaCount"] == 1
    assert values["autoscaling"]["enabled"] is False


def test_helm_deployment_template_uses_recreate() -> None:
    template = (HELM / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    assert re.search(r"strategy:\s*\n\s*type:\s*Recreate", template), (
        "Helm Deployment must use strategy Recreate while the channel is process-local"
    )


def test_deploy_workflow_does_not_default_to_scale_out() -> None:
    """The manual Helm deploy passes --set replicaCount; its defaults must agree."""
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    defaults = re.findall(r"vars\.(?:PROD|STAGING)_REPLICAS \|\| '(\d+)'", text)
    assert defaults, "k8s-deploy.yml no longer resolves replica defaults; update this test"
    assert all(d == "1" for d in defaults), f"k8s-deploy.yml defaults replicas to {defaults}"


# --------------------------------------------------------------------------
# EIE-212-F003 — probes read the signal that means what they check
# --------------------------------------------------------------------------


def _probe_paths(container: dict[str, Any]) -> dict[str, str]:
    return {
        probe: container[probe]["httpGet"]["path"]
        for probe in ("livenessProbe", "readinessProbe", "startupProbe")
        if probe in container
    }


def test_kustomize_probes_split_readiness_from_liveness() -> None:
    deployment = _load(KUSTOMIZE / "base" / "deployment.yaml")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    paths = _probe_paths(container)
    assert paths["readinessProbe"] == READINESS_PATH
    assert paths["livenessProbe"] == LIVENESS_PATH
    assert paths["startupProbe"] == LIVENESS_PATH


def test_helm_probes_split_readiness_from_liveness() -> None:
    template = (HELM / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    probes = dict(
        re.findall(
            r"(livenessProbe|readinessProbe|startupProbe):\s*\n\s*httpGet:\s*\n\s*path:\s*(\S+)",
            template,
        )
    )
    assert probes["readinessProbe"] == READINESS_PATH
    assert probes["livenessProbe"] == LIVENESS_PATH
    assert probes["startupProbe"] == LIVENESS_PATH
