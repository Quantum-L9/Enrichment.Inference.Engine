import asyncio
import json
import random

import httpx

# Non-cryptographic: retry-backoff jitter only; a dedicated instance keeps it explicit.
_RETRY_JITTER = random.Random()


class PerplexityClient:
    """Async Perplexity API client with retry and strict JSON validation."""

    BASE_URL = "https://api.perplexity.ai/chat/completions"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def query(self, prompt: str) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "sonar-reasoning",
            "messages": [
                {"role": "system", "content": "Return strictly valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4000,
        }

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(
                        self.BASE_URL,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    parsed: dict = json.loads(content)
                    return parsed
            except Exception:
                await asyncio.sleep((2**attempt) + _RETRY_JITTER.random())

        raise RuntimeError("Perplexity API failure after 3 attempts")
