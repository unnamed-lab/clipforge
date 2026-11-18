"""Provider contracts for optional keyword and trend research."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from clipforge.packaging.models import KeywordObservation, KeywordResearchProviderStatus


@dataclass(frozen=True)
class ResearchQuery:
    terms: list[str] = field(default_factory=list)
    region: str = "US"
    language: str = "en"
    limit: int = 8
    manual_terms: list[str] = field(default_factory=list)
    now: datetime | None = None

    @property
    def retrieved_at(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def cache_key(self, provider: str) -> str:
        payload = {
            "provider": provider,
            "terms": sorted({term.strip().lower() for term in self.terms if term.strip()}),
            "region": self.region.upper(),
            "language": self.language.lower(),
            "limit": self.limit,
        }
        digest = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()
        return f"research:{provider}:{digest[:32]}"


@dataclass(frozen=True)
class ResearchProviderResult:
    state: str
    provider: str
    observations: list[KeywordObservation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_cache(self) -> dict:
        return {
            "state": self.state,
            "provider": self.provider,
            "observations": [item.model_dump() for item in self.observations],
            "notes": self.notes,
            "limitations": self.limitations,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_cache(cls, payload: dict) -> "ResearchProviderResult":
        return cls(
            state=str(payload.get("state") or "unavailable"),
            provider=str(payload.get("provider") or "unknown"),
            observations=[
                KeywordObservation.model_validate(item)
                for item in payload.get("observations") or []
                if isinstance(item, dict)
            ],
            notes=[str(value) for value in payload.get("notes") or []],
            limitations=[str(value) for value in payload.get("limitations") or []],
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )


class KeywordResearchProvider(Protocol):
    name: str

    def status(self) -> KeywordResearchProviderStatus: ...

    def research(self, query: ResearchQuery) -> ResearchProviderResult: ...
