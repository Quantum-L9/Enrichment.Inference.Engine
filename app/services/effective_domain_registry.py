"""Reader-backed tenant registry; approval audit rows are not runtime authority."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Never

from app.engines.domain_yaml_reader import DomainYamlReader


class EffectiveDomainRegistry(dict[str, dict[str, Any]]):
    """Dictionary-compatible read view over admitted tenants' effective YAML.

    The dictionary stores keys only. Every spec read is a defensive snapshot
    from the reader, including its current publication and integrity checks.
    A missing or corrupt revision is never served from an old API-side copy.
    """

    def __init__(self, reader: DomainYamlReader, tenants: Iterable[str]) -> None:
        super().__init__({tenant: {} for tenant in tenants})
        self.reader = reader

    def __getitem__(self, tenant: str) -> dict[str, Any]:
        if tenant not in self:
            raise KeyError(tenant)
        return self.reader.load(tenant).raw_spec

    def get(self, tenant: str, default: Any = None) -> Any:
        if tenant not in self:
            return default
        return self[tenant]

    def __setitem__(self, tenant: str, spec: dict[str, Any]) -> None:
        raise TypeError("Effective specs are published YAML; registry mutation is forbidden")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Effective specs are published YAML; registry mutation is forbidden")

    def __delitem__(self, tenant: str) -> None:
        raise TypeError("Effective registry membership is bound at startup")

    def clear(self) -> None:
        raise TypeError("Effective registry membership is bound at startup")

    def pop(self, *args: Any) -> Any:
        raise TypeError("Effective registry membership is bound at startup")

    def popitem(self) -> Any:
        raise TypeError("Effective registry membership is bound at startup")

    def setdefault(self, *args: Any) -> Any:
        raise TypeError("Effective registry membership is bound at startup")

    def __or__(self, other: Any) -> Never:
        raise TypeError("Use effective snapshots explicitly; registry union is forbidden")

    def __ior__(self, other: Any) -> Never:
        raise TypeError("Effective specs are published YAML; registry mutation is forbidden")

    def values(self):
        return [self[tenant] for tenant in self]

    def items(self):
        return [(tenant, self[tenant]) for tenant in self]

    def verify_all(self) -> None:
        for tenant in self:
            self.reader.load(tenant)
