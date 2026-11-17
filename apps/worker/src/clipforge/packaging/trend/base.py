"""Provider contracts for optional trend research."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from clipforge.packaging.models import TrendCheckSettings, TrendRecommendation


@dataclass(frozen=True)
class TrendQuery:
    transcript: str = ""
    title: str = ""
    description: str = ""
    topic: str = ""
    subtopics: list[str] = field(default_factory=list)
    genre: str = "auto"
    platform: str = "cross_platform"
    country: str = ""
    region: str = "US"
    audience: str = ""
    language: str = "en"
    niche: str = ""
    freshness_days: int = 30
    limit: int = 8
    now: datetime | None = None

    @property
    def checked_at(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def search_terms(self) -> list[str]:
        values = [
            self.topic,
            self.niche,
            self.title,
            *self.subtopics,
        ]
        terms: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
            key = cleaned.lower()
            if len(cleaned) < 2 or key in seen:
                continue
            seen.add(key)
            terms.append(cleaned)
        return terms[:5]

    def query_text(self) -> str:
        terms = self.search_terms()
        if terms:
            return " ".join(terms[:3])[:240]
        # A transcript is a fallback query only; never send the whole transcript.
        return re.sub(r"\s+", " ", self.transcript or "")[:160]

    def cache_key(self) -> str:
        payload = {
            "topic": self.topic.strip().lower(),
            "subtopics": sorted({value.strip().lower() for value in self.subtopics if value.strip()}),
            "platform": self.platform,
            "region": self.region.upper(),
            "country": self.country.upper(),
            "language": self.language.lower(),
            "niche": self.niche.strip().lower(),
            "freshness_days": self.freshness_days,
            "limit": self.limit,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
        return digest[:32]

    def settings(self) -> TrendCheckSettings:
        return TrendCheckSettings(
            platform=self.platform,
            region=self.region,
            country=self.country,
            language=self.language,
            niche=self.niche,
            audience=self.audience,
            freshness_days=self.freshness_days,
            limit=self.limit,
        )


@dataclass(frozen=True)
class TrendProviderResult:
    state: str
    provider: str
    platform: str
    checked_at: str
    expires_at: str | None = None
    observations: list[TrendRecommendation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def to_cache(self) -> dict:
        return {
            "state": self.state,
            "provider": self.provider,
            "platform": self.platform,
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "observations": [item.model_dump() for item in self.observations],
            "notes": self.notes,
            "limitations": self.limitations,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_cache(cls, payload: dict) -> "TrendProviderResult":
        return cls(
            state=str(payload.get("state") or "unavailable"),
            provider=str(payload.get("provider") or "unknown"),
            platform=str(payload.get("platform") or "unknown"),
            checked_at=str(payload.get("checked_at") or ""),
            expires_at=payload.get("expires_at"),
            observations=[
                TrendRecommendation.model_validate(item)
                for item in payload.get("observations") or []
                if isinstance(item, dict)
            ],
            notes=[str(value) for value in payload.get("notes") or []],
            limitations=[str(value) for value in payload.get("limitations") or []],
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )


class TrendProvider(Protocol):
    name: str
    platform: str

    def status(self) -> dict: ...

    def check(self, query: TrendQuery) -> TrendProviderResult: ...


class UnavailableTrendProvider:
    """An honest placeholder for a platform without official trend access."""

    def __init__(self, *, name: str, platform: str, label: str, detail: str) -> None:
        self.name = name
        self.platform = platform
        self.label = label
        self.detail = detail

    def status(self) -> dict:
        return {
            "provider": self.name,
            "platform": self.platform,
            "label": self.label,
            "operational": False,
            "requires_credentials": True,
            "requires_approval": True,
            "detail": self.detail,
            "manual_research": True,
        }

    def check(self, query: TrendQuery) -> TrendProviderResult:
        return TrendProviderResult(
            state="manual_required",
            provider=self.name,
            platform=self.platform,
            checked_at=query.checked_at.astimezone(timezone.utc).isoformat(),
            notes=[self.detail],
            limitations=[
                "No official trend endpoint is configured for this platform.",
                "No search volume, post count, ranking or growth figure is shown.",
            ],
            error_code="manual_research_required",
            error_message=self.detail,
        )
