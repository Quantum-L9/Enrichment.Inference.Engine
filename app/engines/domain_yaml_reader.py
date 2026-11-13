"""Read EIE-native domain enrichment configuration.

Published immutable YAML is the sole effective authority; PostgreSQL is audit
only. Legacy authoring/test fixtures remain available in nonproduction mode.

L9_META:
  tier: 3
  domain: enrichment
  authority: native-domain-supply-chain-g27r-v3
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

import structlog
import yaml

from app.engines.domain_spec_schema import validate_spec
from app.engines.native_yaml import parse_native_yaml

logger = structlog.get_logger("domain_yaml_reader")

# Identity contract (operator decision 2026-10-22): tenant id == domain spec id
# == Neo4j database name. This is the ONE grammar, also imported by the registry.
# Vertical templates are provenance only; there is no tenant mapping layer.
TENANT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9.-]{2,62}$")
TENANT_ID_RESERVED_PREFIX = "system"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_MAX_SIDECAR_BYTES = 256 * 1024


class DomainPublicationError(ValueError):
    """A tenant's published revision cannot be safely admitted; never fall back."""


def is_legal_tenant_id(value: object) -> bool:
    """Whether a value can name a tenant, its spec and its Neo4j database."""
    if not isinstance(value, str) or value != value.lower():
        return False
    if value.startswith(TENANT_ID_RESERVED_PREFIX):
        return False
    return TENANT_ID_PATTERN.fullmatch(value) is not None


def resolve_cartridge_id(
    tenant: str, *, requested: object = None, configured_default: object = None
) -> str:
    """Which cartridge a request addresses, resolved explicitly at the app boundary.

    In order: the cartridge the request names; the deployment's configured
    default (`DEFAULT_DOMAIN`), when the operator set one; else the tenant's
    own cartridge, which is the identity contract (tenant id == spec id). There
    is no built-in industry default: generic runtime code names no cartridge.
    """
    for candidate in (requested, configured_default):
        if isinstance(candidate, str) and candidate:
            return candidate
    return tenant


def validate_native_spec(raw: object, tenant_id: str) -> dict[str, Any]:
    """Validate a complete EIE-native spec and return a defensive copy."""
    if not is_legal_tenant_id(tenant_id):
        raise ValueError(f"Invalid domain id: {tenant_id!r}")
    return validate_spec(raw, tenant_id)


def _declared_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _declared_version(value: Any) -> str | None:
    # Preserve legacy unquoted numeric YAML versions; a bool is never a version.
    if isinstance(value, bool) or value is None or value == "":
        return None
    return str(value) if isinstance(value, (str, int, float)) else None


def declared_spec_identity(spec: Any) -> tuple[str | None, str | None]:
    """Read declared identity, preserving legacy aliases and top-level precedence.

    Live EIE specs use name/version; old fixtures may use domain_identity or
    domain blocks. Template metadata is provenance, not tenant identity.
    """
    if not isinstance(spec, dict):
        return None, None
    spec_id = _declared_id(spec.get("name"))
    version = _declared_version(spec.get("version"))
    for key in ("domain_identity", "domain"):
        block = spec.get(key)
        if isinstance(block, dict):
            spec_id = spec_id or _declared_id(block.get("id")) or _declared_id(block.get("name"))
            version = version or _declared_version(block.get("version"))
        else:
            spec_id = spec_id or _declared_id(block)
    return spec_id, version


def _json_object(content: bytes) -> dict[str, Any]:
    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DomainPublicationError(f"Duplicate sidecar key: {key}")
            result[key] = value
        return result

    result = json.loads(content, object_pairs_hook=unique_keys)
    if not isinstance(result, dict):
        raise DomainPublicationError("Publication sidecar must be an object")
    return result


def _digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _validate_receipt(receipt: dict[str, Any], tenant: str, sha: str, version: str) -> None:
    expected = {
        "schema": "eie.domain-approval/v1",
        "tenant_id": tenant,
        "sha256": sha,
        "version": version,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise DomainPublicationError(f"Approval receipt {key} mismatch")
    if receipt.get("reviewed_input_sha256") != sha:
        raise DomainPublicationError(
            "Approval receipt reviewed_input_sha256 must bind the exact published bytes"
        )
    for key in ("reviewed_by", "approval_ref", "reviewed_at"):
        if not isinstance(receipt.get(key), str) or not receipt[key].strip():
            raise DomainPublicationError(f"Approval receipt requires {key}")
    reviewed_at = datetime.fromisoformat(receipt["reviewed_at"].replace("Z", "+00:00"))
    if reviewed_at.tzinfo is None or reviewed_at > datetime.now(UTC):
        raise DomainPublicationError(
            "Approval receipt reviewed_at must be timezone-aware and not future"
        )
    if "previous_sha256" not in receipt:
        raise DomainPublicationError(
            "Approval receipt requires previous_sha256 (null for first revision)"
        )
    previous = receipt["previous_sha256"]
    if previous is not None and (not _digest(previous) or previous == sha):
        raise DomainPublicationError("Approval receipt previous_sha256 is invalid")
    if not isinstance(receipt.get("approval_batches"), list):
        raise DomainPublicationError("Approval receipt approval_batches must be a list")
    batches = receipt["approval_batches"]
    if any(
        not isinstance(batch, str) or not batch.strip() or batch != batch.strip()
        for batch in batches
    ):
        raise DomainPublicationError("Approval batches must be explicit nonblank identifiers")
    if len(set(batches)) != len(batches):
        raise DomainPublicationError("Duplicate approval batch identifier")


@dataclass
class EnrichmentHints:
    """Per-node-type enrichment configuration from domain YAML."""

    objective_template: str = ""
    priority_fields: list[str] = field(default_factory=list)
    kb_context: str | None = None
    refresh_interval_days: int = 90
    min_confidence: float = 0.75
    max_variations_discovery: int = 5
    max_variations_targeted: int = 3


@dataclass
class NodeSchema:
    """Schema for a single node type extracted from domain YAML."""

    label: str
    properties: dict[str, str]
    required_fields: list[str] = field(default_factory=list)
    enrichment_hints: EnrichmentHints | None = None


@dataclass
class DomainEnrichmentConfig:
    """A defensive snapshot of one effective enrichment configuration."""

    domain_id: str
    version: str
    node_schemas: dict[str, NodeSchema]
    inference_rules_path: str | None = None
    raw_spec: dict[str, Any] = field(default_factory=dict)
    source_sha256: str | None = None
    approval_ref: str | None = None
    # Directory of the legacy spec file this snapshot was read from. A relative
    # `inference_rules_path` resolves against it, never against the process
    # working directory. Published revisions refuse `inference_rules_path`.
    source_dir: str | None = None


def default_node_label(config: DomainEnrichmentConfig) -> str | None:
    """The node a request that names none addresses: the spec's only node.

    A spec with several nodes has no implicit owner; the caller must name one.
    Never a hard-coded label that the spec may not define.
    """
    if len(config.node_schemas) == 1:
        return next(iter(config.node_schemas))
    return None


def enrichment_hints_for(config: DomainEnrichmentConfig, node_label: str) -> dict[str, Any]:
    """Extract defensive hints from an existing snapshot without another load."""
    node = config.node_schemas.get(node_label)
    if not node or not node.enrichment_hints:
        return {}
    hints = node.enrichment_hints
    return {
        "objective_template": hints.objective_template,
        "priority_fields": list(hints.priority_fields),
        "kb_context": hints.kb_context,
        "refresh_interval_days": hints.refresh_interval_days,
        "min_confidence": hints.min_confidence,
        "max_variations_discovery": hints.max_variations_discovery,
        "max_variations_targeted": hints.max_variations_targeted,
    }


class DomainYamlReader:
    """Read native published revisions, or permissive legacy fixtures when allowed."""

    def __init__(
        self,
        domains_root: str | Path = "domains",
        require_published: bool = False,
        *,
        max_cache_entries: int = 128,
    ) -> None:
        self._root = Path(domains_root).absolute()
        self._require_published = require_published
        if type(max_cache_entries) is not int or max_cache_entries < 1:
            raise ValueError("max_cache_entries must be a positive integer")
        if require_published and (not self._root.is_dir() or self._root.is_symlink()):
            raise DomainPublicationError(f"Published domain root missing or unsafe: {self._root}")
        if require_published:
            try:
                os.close(self._open_root())
            except OSError as exc:
                raise DomainPublicationError(f"Published domain root unsafe: {self._root}") from exc
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[str, tuple[tuple[str, ...], DomainEnrichmentConfig]] = (
            OrderedDict()
        )
        # A pointer disappearing is a publication failure, never permission to
        # resume legacy loading. Invalidation does not forget this boundary.
        self._published_seen: set[str] = set()
        self._lock = RLock()

    @property
    def require_published(self) -> bool:
        """Whether this reader refuses all unpublished/legacy tenant artifacts."""
        return self._require_published

    def invalidate(self, domain_id: str | None = None) -> None:
        """Evict parsed snapshots; publication integrity is checked on every load."""
        with self._lock:
            if domain_id is None:
                self._cache.clear()
            else:
                self._cache.pop(domain_id, None)

    def load(self, domain_id: str) -> DomainEnrichmentConfig:
        """Load one verified snapshot, never serving cached data after a refusal."""
        if not is_legal_tenant_id(domain_id):
            raise ValueError(f"Invalid domain id: {domain_id!r}")
        with self._lock:
            current = self._root / domain_id / "current.json"
            published = (
                self.require_published
                or domain_id in self._published_seen
                or os.path.lexists(current)
            )
            if published:
                self._published_seen.add(domain_id)
                try:
                    return self._load_published(domain_id)
                except (OSError, ValueError, UnicodeError, yaml.YAMLError) as exc:
                    # Retain bounded prior provenance as a receipt-tamper witness.
                    # It is never returned unless a fresh admission succeeds.
                    if isinstance(exc, DomainPublicationError):
                        raise
                    raise DomainPublicationError(
                        f"Published domain {domain_id!r} refused: {exc}"
                    ) from exc
            try:
                spec_path = self._root / domain_id / "spec.yaml"
                if not spec_path.exists():
                    spec_path = self._root / f"{domain_id}.yaml"
                if not spec_path.exists():
                    raise FileNotFoundError(f"Domain spec not found: {spec_path}")
                content = spec_path.read_bytes()
                fingerprint = ("legacy", str(spec_path), hashlib.sha256(content).hexdigest())
                spec_dir = str(spec_path.parent)

                def build_legacy() -> DomainEnrichmentConfig:
                    config = self._parse(domain_id, parse_native_yaml(content))
                    config.source_dir = spec_dir
                    return config

                return self._snapshot(domain_id, fingerprint, build_legacy)
            except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError):
                self._cache.pop(domain_id, None)
                raise

    def _open_root(self) -> int:
        if ".." in self._root.parts:
            raise DomainPublicationError("Published root must not contain path traversal")
        directory = os.open(self._root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in self._root.parts[1:]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
        except OSError:
            os.close(directory)
            raise
        return directory

    def _read_published_file(self, parts: tuple[str, ...]) -> bytes:
        """Read one regular file once, with no-follow directory-relative opens."""
        directory = self._open_root()
        try:
            for part in parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            descriptor = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                maximum = _MAX_SPEC_BYTES if parts[-1] == "spec.yaml" else _MAX_SIDECAR_BYTES
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size > maximum
                ):
                    raise DomainPublicationError(
                        "Published component must be a bounded private regular file"
                    )
                content = source.read(maximum + 1)
                after = os.fstat(source.fileno())
                if len(content) > maximum or (
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise DomainPublicationError("Published component changed while being read")
                return content
        finally:
            os.close(directory)

    def _load_published(self, domain_id: str) -> DomainEnrichmentConfig:
        pointer_parts = (domain_id, "current.json")
        pointer_bytes = self._read_published_file(pointer_parts)
        pointer = _json_object(pointer_bytes)
        if (
            set(pointer) != {"schema", "sha256"}
            or pointer.get("schema") != "eie.domain-current/v1"
            or not _digest(pointer.get("sha256"))
        ):
            raise DomainPublicationError("Invalid published current pointer")
        sha = pointer["sha256"]
        revision = (domain_id, "revisions", sha)
        receipt_parts = (*revision, "receipt.json")
        receipt_bytes = self._read_published_file(receipt_parts)
        content = self._read_published_file((*revision, "spec.yaml"))
        if hashlib.sha256(content).hexdigest() != sha:
            raise DomainPublicationError("Published spec digest mismatch")
        receipt = _json_object(receipt_bytes)
        # Even cache hits validate native shape and receipt against these exact
        # bytes. No mtime-only or TTL-only trust of mutable filesystem content.
        raw = validate_native_spec(parse_native_yaml(content), domain_id)
        _validate_receipt(receipt, domain_id, sha, raw["version"])
        if "inference_rules_path" in raw:
            raise DomainPublicationError(
                "Published inference rules must be inline in the approved YAML"
            )
        metadata = raw.get("metadata", {})
        if "seed" in raw["version"].lower() or (
            isinstance(metadata, dict) and metadata.get("draft_only")
        ):
            raise DomainPublicationError("Seed/draft artifacts cannot be effective published specs")
        if pointer_bytes != self._read_published_file(
            pointer_parts
        ) or receipt_bytes != self._read_published_file(receipt_parts):
            raise DomainPublicationError(
                "Published pointer or receipt changed during admission; retry"
            )
        fingerprint = (
            "published",
            sha,
            hashlib.sha256(receipt_bytes).hexdigest(),
            hashlib.sha256(pointer_bytes).hexdigest(),
        )
        cached = self._cache.get(domain_id)
        if cached and cached[0][:2] == fingerprint[:2] and cached[0][2] != fingerprint[2]:
            raise DomainPublicationError("Immutable approval receipt changed for this revision")

        def build() -> DomainEnrichmentConfig:
            config = self._parse(domain_id, raw)
            config.source_sha256 = sha
            config.approval_ref = receipt["approval_ref"]
            return config

        return self._snapshot(domain_id, fingerprint, build)

    def _snapshot(
        self,
        domain_id: str,
        fingerprint: tuple[str, ...],
        build: Callable[[], DomainEnrichmentConfig],
    ) -> DomainEnrichmentConfig:
        cached = self._cache.get(domain_id)
        if cached is not None and cached[0] == fingerprint:
            self._cache.move_to_end(domain_id)
            return deepcopy(cached[1])
        config = build()
        self._cache[domain_id] = (fingerprint, config)
        self._cache.move_to_end(domain_id)
        while len(self._cache) > self._max_cache_entries:
            self._cache.popitem(last=False)
        logger.info(
            "domain_loaded",
            domain=domain_id,
            nodes=len(config.node_schemas),
            version=config.version,
            source_sha256=config.source_sha256,
        )
        return deepcopy(config)

    def get_target_schema(self, domain_id: str, node_label: str) -> dict[str, str]:
        """Extract target_schema dict for EnrichRequest from a node type."""
        config = self.load(domain_id)
        node = config.node_schemas.get(node_label)
        if not node:
            raise KeyError(f"Node label '{node_label}' not found in domain '{domain_id}'")
        return node.properties

    def get_enrichment_hints(self, domain_id: str, node_label: str) -> dict[str, Any]:
        """Extract enrichment_hints as a defensive plain dict for MetaPromptPlanner."""
        config = self.load(domain_id)
        return enrichment_hints_for(config, node_label)

    def list_domains(self) -> list[str]:
        """List directory tenants and legacy flat-file stems (deduplicated)."""
        directories, flat_files = self._spec_sources()
        return list(dict.fromkeys(directories + [Path(name).stem for name in flat_files]))

    def list_directory_domains(self) -> list[str]:
        """Ids with current.json or spec.yaml; published pointers name tenants too."""
        return self._spec_sources()[0]

    def list_flat_specs(self) -> list[str]:
        """Legacy flat spec file names; production loading still refuses them."""
        return self._spec_sources()[1]

    def _spec_sources(self) -> tuple[list[str], list[str]]:
        directories: list[str] = []
        flat_files: list[str] = []
        if not self._root.exists():
            return directories, flat_files
        for path in sorted(self._root.iterdir()):
            if path.is_dir() and (
                os.path.lexists(path / "current.json") or (path / "spec.yaml").exists()
            ):
                directories.append(path.name)
            elif path.suffix in (".yaml", ".yml") and path.stem != "__pycache__":
                flat_files.append(path.name)
        return directories, flat_files

    def _parse(self, domain_id: str, raw: dict) -> DomainEnrichmentConfig:
        # Deliberately permissive for historical nonproduction fixtures. Native
        # published admission validates first, then uses this same extraction.
        version = declared_spec_identity(raw)[1] or "0.1.0-seed"
        ontology = raw.get("ontology", {})
        nodes_raw = ontology.get("nodes", [])
        node_schemas: dict[str, NodeSchema] = {}
        for node_def in nodes_raw:
            label = node_def.get("label", "")
            props_raw = node_def.get("properties", [])
            props: dict[str, str] = {}
            required: list[str] = []
            for prop in props_raw:
                if isinstance(prop, dict):
                    name = prop.get("name", "")
                    props[name] = prop.get("type", "string")
                    if prop.get("required", False):
                        required.append(name)
                elif isinstance(prop, str):
                    props[prop] = "string"
            hints_raw = node_def.get("enrichment_hints", {})
            hints = None
            if hints_raw:
                hints = EnrichmentHints(
                    objective_template=hints_raw.get("objective_template", ""),
                    priority_fields=hints_raw.get("priority_fields", []),
                    kb_context=hints_raw.get("kb_context"),
                    refresh_interval_days=hints_raw.get("refresh_interval_days", 90),
                    min_confidence=hints_raw.get("min_confidence", 0.75),
                    max_variations_discovery=hints_raw.get("max_variations_discovery", 5),
                    max_variations_targeted=hints_raw.get("max_variations_targeted", 3),
                )
            node_schemas[label] = NodeSchema(
                label=label, properties=props, required_fields=required, enrichment_hints=hints
            )
        return DomainEnrichmentConfig(
            domain_id=domain_id,
            version=version,
            node_schemas=node_schemas,
            inference_rules_path=raw.get("inference_rules_path"),
            raw_spec=raw,
        )
