"""Canary: l9.eie.httpx-async-context-required."""

import httpx


async def fetch(url: str) -> int:
    client = httpx.AsyncClient()
    response = await client.get(url)
    await client.aclose()
    return response.status_code
