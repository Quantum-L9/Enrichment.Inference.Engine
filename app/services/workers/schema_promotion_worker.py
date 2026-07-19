"""
schema_promotion_worker.py
Auto-promotion pipeline for discovered schema fields.

Monitors the schema.field.discovered stream. Fields meeting the promotion
criteria (confidence >= threshold, entity_coverage >= min_coverage) are
promoted: written to the domain KB schema file and a schema.field.promoted
event is emitted.

Stream consumed: schema.field.discovered
Stream produced: schema.field.promoted
Consumer group:  schema-promotion-group
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import redis.asyncio as aioredis
import yaml

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

DISCOVERED_STREAM = "schema.field.discovered"
PROMOTED_STREAM = "schema.field.promoted"
CONSUMER_GROUP = "schema-promotion-group"
CONSUMER_NAME = "schema-promotion-worker-1"

DEFAULT_MIN_CONFIDENCE: float = 0.75
DEFAULT_MIN_COVERAGE: int = 10


class SchemaPromotionWorker:
    """
    Subscribes to schema.field.discovered events and promotes qualifying
    fields to the domain KB YAML schema file.

    Promotion criteria:
      - avg_confidence >= settings.schema_promotion_min_confidence (default 0.75)
      - entity_coverage >= settings.schema_promotion_min_coverage  (default 10)

    On promotion:
      - Appends field definition to config/domain_kb/{domain}/schema.yaml
      - Publishes schema.field.promoted event
    """

    def __init__(self, settings: Settings, kb_root: Path | None = None) -> None:
        self._settings = settings
        self._kb_root = kb_root or Path("config/domain_kb")
        self._redis: aioredis.Redis | None = None
        self._running = False
        self._min_confidence = getattr(
            settings, "schema_promotion_min_confidence", DEFAULT_MIN_CONFIDENCE
        )
        self._min_coverage = getattr(
            settings, "schema_promotion_min_coverage", DEFAULT_MIN_COVERAGE
        )

    async def start(self) -> None:
        self._redis = aioredis.from_url(
            self._settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        try:
            await self._redis.xgroup_create(
                DISCOVERED_STREAM,
                CONSUMER_GROUP,
                id="0",
                mkstream=True,
            )
        except aioredis.ResponseError:
            logger.debug("consumer_group_already_exists", extra={"stream": DISCOVERED_STREAM})
        self._running = True
        logger.info("schema_promotion_worker_started")

    async def stop(self) -> None:
        self._running = False
        if self._redis:
            await self._redis.aclose()

    async def run(self) -> None:
        if self._redis is None:
            msg = "Call start() before run()"
            raise RuntimeError(msg)
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={DISCOVERED_STREAM: ">"},
                    count=20,
                    block=3000,
                )
                if not messages:
                    continue
                for _stream, entries in messages:
                    for msg_id, fields in entries:
                        await self._handle_message(msg_id, fields)
            except asyncio.CancelledError:
                logger.info("schema_promotion_worker_cancelled")
                raise
            except Exception as exc:
                logger.warning("promotion_worker_error", extra={"error": str(exc)})
                await asyncio.sleep(2.0)

    async def _handle_message(self, msg_id: str, fields: dict[str, str]) -> None:
        if self._redis is None:
            return
        try:
            raw: dict[str, Any] = json.loads(fields.get("payload", "{}"))
            field_name: str = raw.get("field_name", "")
            domain: str = raw.get("domain", "")
            avg_confidence: float = float(raw.get("avg_confidence", 0.0))
            entity_coverage: int = int(raw.get("entity_coverage", 0))
            field_type: str = raw.get("field_type", "string")
            sample_values: list[Any] = raw.get("sample_values", [])

            if not field_name or not domain:
                logger.debug("promotion_skip_missing_field_or_domain", extra={"raw": raw})
                await self._redis.xack(DISCOVERED_STREAM, CONSUMER_GROUP, msg_id)
                return

            if avg_confidence < self._min_confidence or entity_coverage < self._min_coverage:
                logger.debug(
                    "promotion_criteria_not_met",
                    extra={
                        "field": field_name,
                        "confidence": avg_confidence,
                        "coverage": entity_coverage,
                    },
                )
                await self._redis.xack(DISCOVERED_STREAM, CONSUMER_GROUP, msg_id)
                return

            promoted = self._promote_field(
                domain=domain,
                field_name=field_name,
                field_type=field_type,
                avg_confidence=avg_confidence,
                entity_coverage=entity_coverage,
                sample_values=sample_values,
            )

            if promoted:
                event = {
                    "payload": json.dumps(
                        {
                            "field_name": field_name,
                            "domain": domain,
                            "field_type": field_type,
                            "avg_confidence": avg_confidence,
                            "entity_coverage": entity_coverage,
                        }
                    )
                }
                # cast: `event` is dict[str, str], a valid xadd field mapping at
                # runtime; the cast only bridges redis-py's invariant typing.
                await self._redis.xadd(
                    PROMOTED_STREAM,
                    cast("dict[Any, Any]", event),
                    maxlen=10000,
                    approximate=True,
                )
                logger.info(
                    "schema_field_promoted",
                    extra={"field": field_name, "domain": domain, "confidence": avg_confidence},
                )

            await self._redis.xack(DISCOVERED_STREAM, CONSUMER_GROUP, msg_id)

        except Exception as exc:
            logger.warning("promotion_handle_error", extra={"msg_id": msg_id, "error": str(exc)})
            if self._redis:
                await self._redis.xack(DISCOVERED_STREAM, CONSUMER_GROUP, msg_id)

    def _promote_field(
        self,
        domain: str,
        field_name: str,
        field_type: str,
        avg_confidence: float,
        entity_coverage: int,
        sample_values: list[Any],
    ) -> bool:
        """Append field to domain schema YAML. Returns True if written."""
        schema_path = self._kb_root / domain / "schema.yaml"
        schema_path.parent.mkdir(parents=True, exist_ok=True)

        if schema_path.exists():
            with schema_path.open() as fh:
                schema: dict[str, Any] = yaml.safe_load(fh) or {}
        else:
            schema = {"fields": {}}

        fields_section: dict[str, Any] = schema.setdefault("fields", {})

        if field_name in fields_section:
            return False

        fields_section[field_name] = {
            "type": field_type,
            "auto_promoted": True,
            "promotion_confidence": round(avg_confidence, 4),
            "promotion_coverage": entity_coverage,
            "sample_values": sample_values[:5],
        }
        schema["fields"] = fields_section

        with schema_path.open("w") as fh:
            yaml.safe_dump(schema, fh, default_flow_style=False, allow_unicode=True)

        return True
