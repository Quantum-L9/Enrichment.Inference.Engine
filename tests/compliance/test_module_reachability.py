"""ARCH-004 — every module under app/ must be reachable or declared.

An entire module with no importer is invisible to every existing gate. ruff's
F401 catches unused imports *within* a file; tools/audit_engine.py rule 12
catches imports that do not resolve — the inverse. Nothing catches a module
that resolves fine and simply nobody imports. That blind spot is how 686
statements of unreachable code accumulated, silently depressing the coverage
ratchet.

Reachability is computed as a STATIC import graph, deliberately not by
importing app.main and inspecting sys.modules. app/main.py performs many of its
imports inside the lifespan function, which never executes on a bare import, so
a runtime probe reports live modules such as services/chassis_handlers.py and
services/event_emitter.py as unreachable. A static graph sees imports wherever
they appear, including inside functions.

A module is reachable when any other module under app/ imports it. Packages
(__init__.py) are exempt: importing app.x.y implicitly loads app.x. Entry
points are exempt because nothing in app/ imports them by design.

To add a module that is intentionally not yet wired, declare it in
STAGED_ARTIFACTS below with a reason and a tracking reference — the same
out-of-band pattern used for DEPRECATED_COMPAT_ARTIFACTS above. Declaring it is
cheap; leaving it undeclared is what this rule prevents.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
APP_DIR = REPO_ROOT / "app"
APP_PKG = "app"

# Modules nothing in app/ imports by design — the process entry point.
ENTRY_POINTS = {
    "app.main",
}

# Native-v3 deliberately exposes publication and pair checking through trusted
# offline operator CLIs, never through a production HTTP or Gate action.
# These are executable import edges, not exemptions or staged-artifact waivers.
OPERATOR_ENTRY_POINTS = {
    "app.services.domain_spec_workflow": "scripts/domain_spec_operator.py",
    "app.engines.domain_spec_compatibility": "scripts/check_domain_compatibility.py",
}

# Modules that exist but are deliberately not wired yet. Each needs a reason
# and a tracking reference so this never becomes a silent dumping ground.
STAGED_ARTIFACTS = {
    "app.engines.convergence.observation_evidence": (
        "Campaign B lane B1. Evidence semantics of request v2.1 observations. Its "
        "caller is the response v2 evidence builder (app/engines/convergence/"
        "field_evidence.py), which lives on the B2 stack (PR #344), not on this "
        "one; the handler seam that passes ResolvedConvergeRequest.observations "
        "to it is wired at integration. Remove this entry in that change."
    ),
    "app.engines.inference.nary_inference_engine": (
        "N-ary inference engine. Wiring needs a KB n-ary fact schema, "
        "hand-authored domain facts, a YAML->NAryFact loader, and reconciliation "
        "of three incompatible InferenceResult shapes. See TODO.md "
        "'n-ary inference enablement'."
    ),
    "app.services.audit_persistence": (
        "Gap-5 audit log. Wiring is blocked on two design decisions: it expects a "
        "raw asyncpg.Pool that this app does not create (pg_store uses a SQLAlchemy "
        "engine), and it performs CREATE TABLE at startup, bypassing Alembic. "
        "See TODO.md Gap-5."
    ),
    "app.engines.graph_sync_client": (
        "EIE-006. Declared T4 transport-contract surface (AGENTS.md C-13, "
        "docs/INVARIANTS.md) whose methods have no caller: the live EIE -> CEG "
        "egress is app/engines/packet_router.py. orchestration_layer.register() "
        "used to construct it at startup and orchestration_layer."
        "run_outcome_feedback was its only reachable entry — itself uncalled — so "
        "the module counted as 'imported' while nothing it offers could run. That "
        "wiring is removed; the module is kept because C-13 names it as part of "
        "the transport contract. Wire it or retire it under C-13, not ad hoc."
    ),
    "app.services.crm.observed_relationship_producer": (
        "QM.POC0.EIE.OBSERVED_RELATIONSHIP_PRODUCER.003. One explicitly invoked "
        "POC producer (operator calls produce_observed_relationship with an "
        "ObservedAssociationKey; the tenant spec declares the association "
        "(observedfrom, QM.POC0.TENANT_ASSOCIATION_MAPPING.004)); deliberately not "
        "wired into enrichment or SideEffectCoordinator until a real tenant declares "
        "a mapping, live HubSpot access exists and CEG's D-9 cross-check lands "
        "(Cognitive.Engine.Graphs#386)."
    ),
}

# Pre-existing unreachable modules, recorded as a shrink-only baseline in the
# same spirit as .l9/baselines/. These were NOT triaged when this rule landed:
# they were invisible to the 0%-coverage audit that found the others, because
# each has a dedicated test importing it directly. Coverage does not prove
# reachability — app/services/outcome_delegator.py is 100% covered and still
# imported by nothing under app/. (This note used to say outcome feedback was
# produced through orchestration_layer.run_outcome_feedback. It was not: that
# function had no caller either, and it is gone — EIE-006.)
#
# This list may only shrink. Adding to it is a rule violation; removing from it
# (by wiring the module up or deleting it) is the point. test_baseline_is_shrink_only
# fails if an entry here has since become reachable and was not removed.
UNREACHABLE_BASELINE = {
    "app.agents.data_hygiene",
    "app.agents.deal_risk",
    "app.agents.handoff",
    "app.agents.lead_router",
    "app.engines.convergence.corrective_retrieval",
    "app.engines.convergence.hierarchical_synthesis",
    "app.score.score_icp_plastics",
    "app.services.contract_enforcement",
    "app.services.inference_rule_registry",
    "app.services.outcome_delegator",
}


def _module_name(path: Path) -> str:
    """Dotted module name for a file under the repo root."""
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _iter_app_modules() -> list[Path]:
    return [
        p for p in sorted(APP_DIR.rglob("*.py")) if "__pycache__" not in p.parts and p.is_file()
    ]


def _resolve_relative(module: str | None, level: int, importer: str, is_package: bool) -> str:
    """Resolve a relative import to an absolute dotted name.

    The anchor differs by importer kind. Inside a plain module such as
    app.main, `from .services import x` resolves against the *parent* package
    (app). Inside a package's __init__.py such as app.services.enrichment.sources,
    the same statement resolves against that package itself. Getting this wrong
    makes live modules look unreachable.
    """
    base = importer.split(".")
    drop = level - 1 if is_package else level
    anchor = base[: len(base) - drop] if drop <= len(base) else base[:1]
    return ".".join([*anchor, module] if module else anchor)


def _imported_names(path: Path, importer: str, is_package: bool) -> set[str]:
    """Every app.* module name this file imports, wherever the import appears."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):  # pragma: no cover - syntax gate covers this
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(f"{APP_PKG}."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            target = (
                _resolve_relative(node.module, node.level, importer, is_package)
                if node.level
                else (node.module or "")
            )
            if not target.startswith(f"{APP_PKG}."):
                continue
            found.add(target)
            # `from app.pkg import mod` also reaches app.pkg.mod
            for alias in node.names:
                found.add(f"{target}.{alias.name}")
    return found


def _build_import_graph() -> tuple[set[str], set[str]]:
    """Return (all module names, names imported by at least one app/ module)."""
    files = _iter_app_modules()
    all_modules = {_module_name(p) for p in files}
    imported: set[str] = set()
    for path in files:
        imported |= _imported_names(path, _module_name(path), is_package=path.name == "__init__.py")
    for cli in OPERATOR_ENTRY_POINTS.values():
        path = REPO_ROOT / cli
        imported |= _imported_names(path, _module_name(path), is_package=False)
    return all_modules, imported


def test_operator_entry_points_are_real_import_edges() -> None:
    """The explicitly authorized offline CLIs must import their claimed owner."""
    for module, relative_path in OPERATOR_ENTRY_POINTS.items():
        path = REPO_ROOT / relative_path
        assert path.is_file(), f"Operator CLI missing: {relative_path}"
        assert module in _imported_names(path, _module_name(path), is_package=False)


def test_staged_artifacts_all_exist() -> None:
    """A stale STAGED_ARTIFACTS entry would silently exempt nothing."""
    all_modules, _ = _build_import_graph()
    stale = sorted(set(STAGED_ARTIFACTS) - all_modules)
    assert stale == [], (
        f"STAGED_ARTIFACTS names modules that no longer exist: {stale}. Remove the entries."
    )


def test_staged_artifacts_have_reasons() -> None:
    for module, reason in STAGED_ARTIFACTS.items():
        assert reason and len(reason) > 40, (
            f"{module} must carry a substantive reason and tracking reference"
        )


def test_no_undeclared_unreachable_modules() -> None:
    """ARCH-004: no module under app/ may be unimported and undeclared."""
    all_modules, imported = _build_import_graph()

    package_names = {_module_name(p) for p in _iter_app_modules() if p.name == "__init__.py"}
    exempt = package_names | ENTRY_POINTS | set(STAGED_ARTIFACTS) | UNREACHABLE_BASELINE

    unreachable = sorted(m for m in all_modules - imported if m not in exempt)

    assert unreachable == [], (
        "Modules under app/ that no other app/ module imports:\n  "
        + "\n  ".join(unreachable)
        + "\n\nAn unimported module is dead weight in the coverage denominator and "
        "invisible to every other gate. Either wire it up, delete it, or declare it "
        "in STAGED_ARTIFACTS in this file with a reason and a tracking reference. "
        "Do NOT add it to UNREACHABLE_BASELINE — that list is shrink-only."
    )


def test_baseline_is_shrink_only() -> None:
    """A baseline entry that became reachable must be removed from the list.

    Without this, the baseline would silently retain entries that no longer
    describe anything, and the rule would weaken over time instead of
    tightening.
    """
    all_modules, imported = _build_import_graph()

    now_reachable = sorted(m for m in UNREACHABLE_BASELINE if m in imported)
    assert now_reachable == [], (
        "These modules are now imported and must be removed from "
        f"UNREACHABLE_BASELINE: {now_reachable}"
    )

    deleted = sorted(m for m in UNREACHABLE_BASELINE if m not in all_modules)
    assert deleted == [], (
        f"These modules no longer exist and must be removed from UNREACHABLE_BASELINE: {deleted}"
    )
