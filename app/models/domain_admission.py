"""Admitted domain ids for contract payloads: the loaded specs, never a literal.

A payload's ``domain`` names a tenant spec (tenant id == domain spec id). It is
admitted only when the effective spec reader loads that spec -- the same reader
contract ``app.main`` builds: published revisions in tenant-serving
environments, the checked-in specs in local/dev/test. Any spec works; no
contract model hard-codes one.

L9_META:
  tier: 2
  domain: enrichment
  authority: owner decision 2026-11-11 (the domain spec is the cartridge)
"""

from __future__ import annotations

from functools import lru_cache

from app.engines.domain_yaml_reader import DomainYamlReader, is_legal_tenant_id

_UNPUBLISHED_ENVIRONMENTS = frozenset({"local", "dev", "test"})


@lru_cache(maxsize=4)
def _reader(domains_dir: str, require_published: bool) -> DomainYamlReader:
    return DomainYamlReader(domains_dir, require_published=require_published)


def effective_reader() -> DomainYamlReader:
    """The process's effective spec reader, configured exactly as ``app.main`` does."""
    from app.core.config import get_settings

    settings = get_settings()
    return _reader(
        settings.domains_dir,
        settings.l9_environment not in _UNPUBLISHED_ENVIRONMENTS,
    )


def admitted_domain(value: object, reader: DomainYamlReader | None = None) -> str:
    """``value`` when it names a domain spec the reader admits, else ``ValueError``."""
    if not isinstance(value, str) or not is_legal_tenant_id(value):
        msg = f"domain {value!r} is not a legal domain spec id"
        raise ValueError(msg)
    try:
        (reader or effective_reader()).load(value)
    except (OSError, ValueError, TypeError) as exc:
        msg = f"domain {value!r} is not an admitted domain spec: {exc}"
        raise ValueError(msg) from exc
    return value
