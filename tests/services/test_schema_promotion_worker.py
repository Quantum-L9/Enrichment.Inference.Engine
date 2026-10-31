"""Focused Redis stream boundary tests for the schema promotion worker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.workers.schema_promotion_worker import (
    CONSUMER_GROUP,
    CONSUMER_NAME,
    DISCOVERED_STREAM,
    PROMOTED_STREAM,
    SchemaPromotionWorker,
)


def _worker(kb_root: Path) -> SchemaPromotionWorker:
    return SchemaPromotionWorker(
        settings=SimpleNamespace(redis_url="redis://localhost:6379"),
        kb_root=kb_root,
    )


@pytest.mark.asyncio
async def test_run_dispatches_decoded_xreadgroup_entries(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    redis_client = AsyncMock()
    worker._redis = redis_client
    worker._running = True
    redis_client.xreadgroup.side_effect = [
        [(DISCOVERED_STREAM, [("1-0", {"payload": "{}"})])],
        asyncio.CancelledError(),
    ]
    worker._handle_message = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await worker.run()

    redis_client.xreadgroup.assert_awaited_with(
        groupname=CONSUMER_GROUP,
        consumername=CONSUMER_NAME,
        streams={DISCOVERED_STREAM: ">"},
        count=20,
        block=3000,
    )
    worker._handle_message.assert_awaited_once_with("1-0", {"payload": "{}"})


@pytest.mark.asyncio
async def test_promoted_message_preserves_xadd_stream_arguments(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    redis_client = AsyncMock()
    worker._redis = redis_client

    await worker._handle_message(
        "1-0",
        {
            "payload": json.dumps(
                {
                    "field_name": "recycling_rate",
                    "domain": "plastics",
                    "field_type": "number",
                    "avg_confidence": 0.9,
                    "entity_coverage": 12,
                    "sample_values": [0.8],
                }
            )
        },
    )

    args, kwargs = redis_client.xadd.await_args
    assert args[0] == PROMOTED_STREAM
    assert json.loads(args[1]["payload"]) == {
        "field_name": "recycling_rate",
        "domain": "plastics",
        "field_type": "number",
        "avg_confidence": 0.9,
        "entity_coverage": 12,
    }
    assert kwargs == {"maxlen": 10_000, "approximate": True}
