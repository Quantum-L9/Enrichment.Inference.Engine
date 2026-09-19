"""
CRM source boundary — the ingestion seam that feeds ``scan_crm_fields``.

A CRM source obtains live field metadata (and bounded representative samples)
from a customer's CRM and normalizes them into the existing ``CRMField``
representation. It is an ingestion adapter only: it owns no matching, ranking,
scoring, or domain-compilation logic — those stay in the scanner and in Gate/CEG.

L9 Contract Compliance:
  - No FastAPI imports
  - No eval/exec
  - No stubs or TODOs
"""

from __future__ import annotations

from typing import Protocol

from .crm_field_scanner import CRMField

#: Default number of records sampled per source resource.
DEFAULT_SAMPLE_LIMIT = 25
#: Hard upper bound on records sampled per source resource (enforced in code).
MAX_SAMPLE_LIMIT = 100


class CRMSourceError(RuntimeError):
    """A CRM source could not be read.

    Wraps connection failures, timeouts, non-2xx responses, invalid JSON,
    malformed method responses, and inaccessible mandatory resources.
    Messages identify provider / resource / method and never carry credentials
    or raw response bodies.
    """


class CRMSource(Protocol):
    """Smallest useful source contract: produce the live ``CRMField`` list."""

    async def fields(self, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> list[CRMField]: ...
