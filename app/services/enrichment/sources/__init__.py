"""
Enrichment sources registry.

Each source implements the BaseSource interface and can be used within
the WaterfallEngine for multi-source enrichment with quality-based fallback.

Available sources:
- PerplexitySonarSource: Wraps existing perplexity_client (primary)
- ClearbitSource: Company + contact enrichment via Clearbit API
- ZoomInfoSource: Company + contact enrichment via ZoomInfo API
- ApolloSource: Company + contact enrichment via Apollo.io API
- HunterSource: Contact email verification via Hunter.io API
- OpenAISource: Generative enrichment via the OpenAI chat-completions API
- AnthropicSource: Generative enrichment via the Anthropic Messages API
"""

from .anthropic_adapter import AnthropicSource
from .apollo import ApolloSource
from .base import BaseSource, EnrichmentResult, SourceConfig
from .clearbit import ClearbitSource
from .hunter import HunterSource
from .llm_base import LLMSource
from .openai_adapter import OpenAISource
from .perplexity_adapter import PerplexitySonarSource
from .zoominfo import ZoomInfoSource

# Source class registry — maps config names to implementations
SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    "perplexity_sonar": PerplexitySonarSource,
    "clearbit": ClearbitSource,
    "zoominfo": ZoomInfoSource,
    "apollo": ApolloSource,
    "hunter": HunterSource,
    "openai": OpenAISource,
    "anthropic": AnthropicSource,
}

__all__ = [
    "AnthropicSource",
    "ApolloSource",
    "BaseSource",
    "ClearbitSource",
    "EnrichmentResult",
    "HunterSource",
    "LLMSource",
    "OpenAISource",
    "PerplexitySonarSource",
    "SOURCE_REGISTRY",
    "SourceConfig",
    "ZoomInfoSource",
]
