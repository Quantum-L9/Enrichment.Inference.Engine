"""
OpenAI enrichment source adapter.

Wraps the existing ``OpenAIClient`` as a ``BaseSource`` implementation so the
WaterfallEngine can dispatch to OpenAI alongside Perplexity and the CRM
sources. This adapter does NOT duplicate the client's HTTP, retry or circuit
breaker logic — it delegates to app/services/openai_client.py.

Declared in docs/contracts/dependencies/openai.yaml and asserted by
tests/contracts/test_dependency_contracts.py; this adapter is what makes that
declaration true at runtime.

L9 Architecture Note:
    This module bridges the existing OpenAI client into the multi-source
    enrichment interface. It never imports FastAPI.
"""

from __future__ import annotations

from ...openai_client import OpenAIClient
from .llm_base import LLMSource


class OpenAISource(LLMSource):
    """BaseSource adapter for the OpenAI chat-completions API."""

    client_class = OpenAIClient
