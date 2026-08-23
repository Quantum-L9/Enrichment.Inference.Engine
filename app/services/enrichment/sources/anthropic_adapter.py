"""
Anthropic enrichment source adapter.

Wraps the existing ``AnthropicClient`` as a ``BaseSource`` implementation so the
WaterfallEngine can dispatch to Anthropic alongside Perplexity and the CRM
sources. This adapter does NOT duplicate the client's HTTP, retry or circuit
breaker logic — it delegates to app/services/anthropic_client.py.

Declared in docs/contracts/dependencies/anthropic.yaml and asserted by
tests/contracts/test_dependency_contracts.py; this adapter is what makes that
declaration true at runtime.

L9 Architecture Note:
    This module bridges the existing Anthropic client into the multi-source
    enrichment interface. It never imports FastAPI.
"""

from __future__ import annotations

from ...anthropic_client import AnthropicClient
from .llm_base import LLMSource


class AnthropicSource(LLMSource):
    """BaseSource adapter for the Anthropic Messages API."""

    client_class = AnthropicClient
