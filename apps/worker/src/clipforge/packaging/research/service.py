"""Free-first keyword research orchestration with cache and honest fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from clipforge.config import Settings
from clipforge.packaging.brief import _content_tokens
from clipforge.packaging.models import (
    ContentBrief,
    KeywordObservation,
    KeywordResearchProviderStatus,
    KeywordResearchResult,
)
from clipforge.packaging.research.base import ResearchProviderResult, ResearchQuery
from clipforge.packaging.research.providers import (
    GoogleTrendsBridgeProvider,
    ManualResearchProvider,
    VidIQProvider,
    YouTubeAutocompleteProvider,
    YouTubePublicVideoResearch,
)

logger = logging.getLogger(__name__)

RESEARCH_NOTE = "Research informs terminology only; it does not prove demand or guarantee reach."


class KeywordResearchRegistry:
    def __init__(self, free: list, enhanced: list) -> None:
        self.free = free
        self.enhanced = enhanced

    def providers(self, mode: str) -> list:
        if mode == "off":
            return []
        if mode == "enhanced":
            # Enhanced includes free providers plus optional enrichment adapters.
            return [*self.free, *[item for item in self.enhanced if item not in self.free]]
        return list(self.free)

    def statuses(self, mode: str = "free") -> list[KeywordResearchProviderStatus]:
        seen: set[str] = set()
        statuses: list[KeywordResearchProviderStatus] = []
        for provider in self.providers(mode):
            if provider.name in seen:
                continue
            seen.add(provider.name)
            statuses.append(provider.status())
        for provider in self.enhanced:
            if provider.name in seen:
                continue
            seen.add(provider.name)
            statuses.append(provider.status())
        return statuses


def build_research_registry(settings: Settings, trend_provider) -> KeywordResearchRegistry:
    free: list = [ManualResearchProvider()]
    free.append(YouTubeAutocompleteProvider(settings.youtube_suggest_enabled))
    if getattr(trend_provider, "api_key", ""):
        free.append(YouTubePublicVideoResearch(trend_provider))
    free.append(
        GoogleTrendsBridgeProvider(
            getattr(settings, "trends_endpoint", ""),
            getattr(settings, "trends_api_key", ""),
        )
    )
    enhanced: list = [VidIQProvider(getattr(settings, "vidiq_enabled", False))]
    return KeywordResearchRegistry(free=free, enhanced=enhanced)


class KeywordResearchService:
    def __init__(self, *, cache, registry: KeywordResearchRegistry, settings: Settings) -> None:
        self.cache = cache
        self.registry = registry
        self.settings = settings

    def config(self) -> dict:
        return {
            "providers": [item.model_dump() for item in self.registry.statuses("enhanced")],
            "modes": {
                "off": "Source content only. No external calls.",
                "free": "Free providers where configured: manual terms, opt-in YouTube suggestions, official YouTube public metadata, approved Trends bridge.",
                "enhanced": "Free providers plus optional enrichment adapters such as vidIQ, when a verified connection exists.",
            },
            "note": RESEARCH_NOTE,
        }

    def research(
        self,
        brief: ContentBrief,
        *,
        mode: str = "off",
        manual_terms: list[str] | None = None,
        region: str = "US",
        language: str = "en",
        force: bool = False,
    ) -> KeywordResearchResult:
        now = datetime.now(timezone.utc)
        if mode == "off":
            return KeywordResearchResult(
                mode="off",
                status="disabled",
                retrieved_at=now.isoformat(),
                providers=[item.model_dump() for item in self.registry.statuses("enhanced")],
                notes=["No external research was requested. Packaging is based on the source content."],
                limitations=[RESEARCH_NOTE],
            )

        terms = research_terms(brief)
        query = ResearchQuery(
            terms=terms,
            region=region or "US",
            language=language or "en",
            limit=8,
            manual_terms=list(manual_terms or []),
            now=now,
        )
        observations: list[KeywordObservation] = []
        notes: list[str] = []
        limitations: list[str] = []
        states: list[str] = []
        cached_any = False
        providers = self.registry.providers(mode)
        for provider in providers:
            key = query.cache_key(provider.name)
            cached = None if force else self.cache.get(key)
            if cached:
                expires = _date(cached.get("expires_at"))
                if expires is not None and now <= expires:
                    result = ResearchProviderResult.from_cache(cached.get("result") or {})
                    result = ResearchProviderResult(
                        state="cached",
                        provider=result.provider,
                        observations=result.observations,
                        notes=[*result.notes, "Served from the research cache; no new provider call was made."],
                        limitations=result.limitations,
                    )
                    cached_any = True
                else:
                    cached = None
            if cached is None:
                try:
                    result = provider.research(query)
                except Exception as exc:  # noqa: BLE001 - research must never block packaging
                    logger.warning("Research provider %s failed: %s", provider.name, exc)
                    result = ResearchProviderResult(
                        state="unavailable",
                        provider=provider.name,
                        notes=["The provider failed; packaging continues without its data."],
                        limitations=["No metric is shown for a failed provider."],
                        error_code="provider_error",
                        error_message=str(exc)[:240],
                    )
                expires = now + timedelta(seconds=self.settings.trend_cache_ttl_seconds)
                if result.state in {"live", "no_results"}:
                    self.cache.put(
                        key,
                        provider=provider.name,
                        platform="keyword_research",
                        query={"terms": terms, "region": query.region, "language": query.language},
                        result=result.to_cache(),
                        checked_at=now.isoformat(),
                        expires_at=expires.isoformat(),
                    )
            states.append(result.state)
            observations.extend(result.observations)
            notes.extend(result.notes)
            limitations.extend(result.limitations)
            provider_status = provider.status()
            limitations.extend(provider_status.limitations)
            if not result.observations:
                notes.extend(provider_status.detail.split(".")[:1])

        deduped: list[KeywordObservation] = []
        seen: set[tuple[str, str, str]] = set()
        for item in observations:
            key = (item.provider, item.kind, item.term.lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        suggested: list[str] = []
        for item in deduped:
            if item.kind not in {"suggestion", "manual", "relative_interest"}:
                continue
            value = " ".join(item.term.split())[:80]
            if len(value) < 2 or value.startswith("#"):
                continue
            if value.lower() in {entry.lower() for entry in suggested}:
                continue
            suggested.append(value)
        suggested = suggested[:12]

        status = _combined_state(states, bool(deduped))
        return KeywordResearchResult(
            mode="enhanced" if mode == "enhanced" else "free",
            status=status,  # type: ignore[arg-type]
            retrieved_at=now.isoformat(),
            providers=[item.model_dump() for item in self.registry.statuses("enhanced")],
            observations=deduped,
            suggested_terms=suggested,
            terms_used=terms,
            notes=list(dict.fromkeys([*notes, "Research used only terms already grounded in the clip or supplied manually."])),
            limitations=list(
                dict.fromkeys(
                    [
                        *limitations,
                        RESEARCH_NOTE,
                        "A provider suggestion is not a measured search volume or trend score.",
                    ]
                )
            ),
            cached=cached_any and status == "cached",
        )


def research_terms(brief: ContentBrief) -> list[str]:
    values: list[str] = []
    candidates = [
        brief.clip_topic,
        *brief.search_concepts[:4],
        *brief.entities[:2],
        brief.subject,
    ]
    blocked = {"clip", "this clip", "video", "shorts", "reels", "tiktok", "youtube"}
    for value in candidates:
        cleaned = " ".join(str(value or "").split())[:120]
        if len(cleaned) < 3 or cleaned.lower() in blocked:
            continue
        if not _content_tokens(cleaned):
            continue
        if cleaned.lower() in {item.lower() for item in values}:
            continue
        values.append(cleaned)
    return values[:3]


def _combined_state(states: list[str], has_results: bool) -> str:
    if not states:
        return "no_results"
    if "live" in states or "cached" in states:
        return "live" if "live" in states else "cached"
    for state in ("quota_exhausted", "rate_limited", "auth_missing", "manual_required", "unavailable"):
        if state in states and not has_results:
            return state
    return "no_results" if not has_results else "live"


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
