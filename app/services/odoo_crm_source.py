"""
Odoo CRM source adapter — read-only Odoo 19 JSON-2 discovery of Contacts + CRM.

Inspects exactly two Odoo resources, independently and both mandatorily:

    res.partner   (Contacts / companies)
    crm.lead      (CRM leads / opportunities)

For each resource it makes exactly two calls — ``fields_get`` for the live
model schema and one bounded ``search_read`` for representative samples — and
converts the result into ``CRMField`` objects whose ``source_resource`` records
which model supplied each field. Fields with identical technical names across
models (``res.partner.phone`` vs ``crm.lead.phone``) are preserved independently.

Transport: Odoo 19 JSON-2 external API (``POST {base_url}/json/2/<model>/<method>``,
bearer API-key authentication, keyword parameters as the JSON body). Odoo's
normal ACLs, record rules and field access apply to the integration identity;
this adapter never bypasses them and invokes no mutating method.

L9 Contract Compliance:
  - No FastAPI imports
  - No eval/exec
  - No stubs or TODOs
  - structlog for structured logging (C-04); never logs sample values or credentials
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
import structlog

from .crm_field_scanner import CRMField
from .crm_source import DEFAULT_SAMPLE_LIMIT, MAX_SAMPLE_LIMIT, CRMSourceError

logger = structlog.get_logger(__name__)

SOURCE_SYSTEM = "odoo"

#: The only Odoo models this adapter may address. Both are mandatory.
ODOO_CRM_RESOURCES: tuple[str, ...] = ("res.partner", "crm.lead")

#: The only Odoo methods this adapter may invoke — read/introspection only.
_ALLOWED_METHODS: frozenset[str] = frozenset({"fields_get", "search_read"})

_FIELDS_GET_ATTRIBUTES = ["string", "type", "help", "required", "readonly", "relation", "store"]

#: Field types safe to include in the bounded sample read. Binary, one2many and
#: reference fields are excluded (large, relational, or irrelevant for samples).
_SAMPLEABLE_TYPES: frozenset[str] = frozenset(
    {
        "char",
        "text",
        "html",
        "selection",
        "boolean",
        "integer",
        "float",
        "monetary",
        "date",
        "datetime",
        "many2one",
        "many2many",
    }
)

_MAX_SAMPLE_VALUES_PER_FIELD = 5
_REQUEST_TIMEOUT_SECONDS = 10.0


def _user_agent() -> str:
    """``User-Agent`` recommended by the Odoo JSON-2 docs; version owned by pyproject."""
    try:
        return f"enrichment-inference-engine/{version('domain-enrichment-api')}"
    except PackageNotFoundError:
        return "enrichment-inference-engine/unknown"


_USER_AGENT = _user_agent()


def _sampleable(field_meta: dict[str, Any]) -> bool:
    return str(field_meta.get("type", "")) in _SAMPLEABLE_TYPES


def _is_populated(value: Any, field_type: str) -> bool:
    """Odoo returns ``False`` for empty char/relational fields; a boolean ``False`` is real."""
    if field_type == "boolean":
        return value is not None
    # Identity check for False: ``0 == False`` in Python, and a numeric 0 is a real value.
    if value is None or value is False:
        return False
    return value not in ("", [], {})


class OdooCRMSource:
    """Read-only Odoo 19 JSON-2 source for ``res.partner`` and ``crm.lead`` fields."""

    RESOURCES: tuple[str, ...] = ODOO_CRM_RESOURCES

    def __init__(self, *, base_url: str, api_key: str, database: str = "") -> None:
        if not base_url or not api_key:
            raise CRMSourceError("Odoo CRM discovery requires ODOO_URL and ODOO_API_KEY")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._database = database

    # ── Public contract ──────────────────────────────────────────

    async def fields(self, *, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> list[CRMField]:
        if not 1 <= sample_limit <= MAX_SAMPLE_LIMIT:
            raise CRMSourceError(
                f"Odoo sample_limit must be between 1 and {MAX_SAMPLE_LIMIT}, got {sample_limit}"
            )
        logger.info(
            "odoo_crm_discovery_started",
            resources=list(self.RESOURCES),
            sample_limit=sample_limit,
        )
        headers = {
            "Authorization": f"bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._database:
            headers["X-Odoo-Database"] = self._database

        collected: list[CRMField] = []
        try:
            async with httpx.AsyncClient(
                headers=headers, timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
            ) as client:
                for model in self.RESOURCES:
                    collected.extend(await self._fetch_resource(client, model, sample_limit))
        except CRMSourceError as exc:
            logger.warning("odoo_crm_discovery_failed", error=str(exc))
            raise

        collected.sort(key=lambda f: (f.source_resource or "", f.name))
        logger.info("odoo_crm_discovery_complete", field_count=len(collected))
        return collected

    # ── Private helpers ──────────────────────────────────────────

    async def _call(
        self, client: httpx.AsyncClient, model: str, method: str, payload: dict[str, Any]
    ) -> Any:
        if model not in self.RESOURCES or method not in _ALLOWED_METHODS:
            raise CRMSourceError(f"Odoo source call refused: model={model} method={method}")
        url = f"{self._base_url}/json/2/{model}/{method}"
        try:
            response = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise CRMSourceError(
                f"Odoo source call timed out: model={model} method={method}"
            ) from exc
        except httpx.HTTPError as exc:
            raise CRMSourceError(
                f"Odoo source call failed: model={model} method={method} "
                f"reason={type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise CRMSourceError(
                f"Odoo source call failed: model={model} method={method} "
                f"status={response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CRMSourceError(
                f"Odoo source returned invalid JSON: model={model} method={method}"
            ) from exc

    async def _fetch_resource(
        self, client: httpx.AsyncClient, model: str, sample_limit: int
    ) -> list[CRMField]:
        metadata = await self._call(
            client, model, "fields_get", {"attributes": _FIELDS_GET_ATTRIBUTES}
        )
        if not isinstance(metadata, dict) or not all(
            isinstance(meta, dict) for meta in metadata.values()
        ):
            raise CRMSourceError(
                f"Odoo source returned unexpected shape: model={model} method=fields_get"
            )

        sampleable = sorted(name for name, meta in metadata.items() if _sampleable(meta))
        rows = await self._call(
            client,
            model,
            "search_read",
            {"domain": [], "fields": sampleable, "limit": sample_limit},
        )
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise CRMSourceError(
                f"Odoo source returned unexpected shape: model={model} method=search_read"
            )

        fields: list[CRMField] = []
        for name, meta in metadata.items():
            field_type = str(meta.get("type", "string"))
            samples: list[Any] = []
            populated = 0
            if name in sampleable:
                for row in rows:
                    value = row.get(name)
                    if not _is_populated(value, field_type):
                        continue
                    populated += 1
                    if len(samples) < _MAX_SAMPLE_VALUES_PER_FIELD and value not in samples:
                        samples.append(value)
            fill_rate = round(populated / len(rows), 4) if rows and name in sampleable else None
            fields.append(
                CRMField(
                    name=name,
                    field_type=field_type,
                    sample_values=samples,
                    fill_rate=fill_rate,
                    source_system=SOURCE_SYSTEM,
                    source_resource=model,
                )
            )

        logger.info(
            "odoo_crm_resource_discovered",
            model=model,
            field_count=len(fields),
            sample_count=len(rows),
        )
        return fields
