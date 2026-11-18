"""Optional keyword and trend research adapters."""

from clipforge.packaging.research.base import (
    KeywordResearchProvider,
    ResearchProviderResult,
    ResearchQuery,
)
from clipforge.packaging.research.service import (
    KeywordResearchRegistry,
    KeywordResearchService,
    build_research_registry,
    research_terms,
)

__all__ = [
    "KeywordResearchProvider",
    "KeywordResearchRegistry",
    "KeywordResearchService",
    "ResearchProviderResult",
    "ResearchQuery",
    "build_research_registry",
    "research_terms",
]
