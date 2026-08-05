"""
Explicit Gate registration (TASK-003).

The app is built with ``auto_register_with_gate=False`` so registration is
performed here, explicitly, during startup. The payload is built in-process
rather than via the Gate SDK helper because the SDK's
``build_registration_payload`` does not emit ``metadata.owner`` and defaults
``health_endpoint`` to ``/v1/health`` — EIE serves health at ``/api/v1/health``
and must own the ``converge`` / ``graph-inference-result`` actions.

Registration is NON-FATAL: every failure path returns ``False`` and never
raises, so a missing or unreachable Gate cannot crash node startup.

Gate contract (verified against Constellation.Gate + Gate_SDK):
  - POST ``{gate_url}/v1/admin/register?overwrite=true``
  - ``X-Admin-Token`` header only when a token is configured
  - Body is a JSON object keyed by node_name (NodeRegistrationInput forbids
    extra keys). There is NO ``execute_path`` (Gate appends ``/v1/execute`` to
    ``internal_url``), NO ``health_path`` (use ``health_endpoint``), and NO
    top-level ``owner`` (derived from ``metadata.owner`` first).
"""

from __future__ import annotations

import structlog

from app.core.config import Settings

logger = structlog.get_logger("gate_registration")

NODE_NAME = "enrichment-engine"
SUPPORTED_ACTIONS = ["converge", "graph-inference-result", "enrich", "enrich-and-sync"]
HEALTH_ENDPOINT = "/api/v1/health"
_DEFAULT_INTERNAL_URL = f"http://{NODE_NAME}:8000"
NODE_VERSION = "2.3.0"


def build_payload(settings: Settings) -> dict[str, dict[str, object]]:
    """Build the Gate admin-registration body keyed by node name."""
    internal_url = (settings.gate_internal_url or _DEFAULT_INTERNAL_URL).strip().rstrip("/")
    return {
        NODE_NAME: {
            "internal_url": internal_url,
            "supported_actions": list(SUPPORTED_ACTIONS),
            "health_endpoint": HEALTH_ENDPOINT,
            "metadata": {
                "owner": "eie",
                "version": NODE_VERSION,
                "type": "enrichment",
            },
        }
    }


async def register_with_gate(settings: Settings) -> bool | None:
    """Register this node with the Gate.

    Returns:
        None  — registration disabled or no gate_url configured (no-op).
        True  — Gate accepted the registration (HTTP 200).
        False — Gate rejected it, or any error occurred (non-fatal).
    """
    if not settings.gate_registration_enabled or not settings.gate_url:
        return None

    # Imported lazily so this module has no hard httpx import at collection time.
    import httpx

    payload = build_payload(settings)
    url = f"{settings.gate_url.rstrip('/')}/v1/admin/register"
    headers = {"Content-Type": "application/json"}
    if settings.gate_admin_token:
        headers["X-Admin-Token"] = settings.gate_admin_token

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                params={"overwrite": "true"},
            )
        if response.status_code == 200:
            logger.info("gate_registration_ok", node=NODE_NAME, url=url)
            return True
        logger.warning(
            "gate_registration_rejected",
            node=NODE_NAME,
            url=url,
            status_code=response.status_code,
        )
        return False
    except Exception as exc:
        logger.warning("gate_registration_error", node=NODE_NAME, url=url, error=str(exc))
        return False
