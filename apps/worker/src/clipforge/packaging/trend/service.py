"""Trend checking with cache, stale fallback and honest provider states."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from clipforge.config import Settings
from clipforge.packaging.models import (
    ManualTrend,
    TrendCheckResult,
    TrendRecommendation,
)
from clipforge.packaging.trend.base import TrendProviderResult, TrendQuery
from clipforge.packaging.trend.ranking import rank_trends
from clipforge.packaging.trend.registry import TrendProviderRegistry

logger = logging.getLogger(__name__)


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class TrendService:
    def __init__(
        self,
        *,
        cache,
        registry: TrendProviderRegistry,
        settings: Settings,
    ) -> None:
        self.cache = cache
        self.registry = registry
        self.settings = settings

    def config(self) -> dict:
        return self.registry.config()

    def check(
        self,
        query: TrendQuery,
        *,
        dismissed_ids: set[str] | None = None,
        weights: dict[str, float] | None = None,
        force: bool = False,
    ) -> TrendCheckResult:
        provider = self.registry.get(query.platform)
        cache_key = f"{provider.name}:{query.platform}:{query.cache_key()}"
        cached = self.cache.get(cache_key)
        now = query.checked_at.astimezone(timezone.utc)

        if cached and not force:
            expires_at = _date(cached.get("expires_at"))
            if expires_at is not None and now <= expires_at:
                cached_result = TrendProviderResult.from_cache(cached.get("result") or {})
                return self._result(
                    cached_result,
                    query,
                    dismissed_ids=dismissed_ids,
                    weights=weights,
                    state="cached",
                    cached=True,
                    stale=False,
                    notes=["Served from the provider-aware cache; no new API call was made."],
                )

        result = provider.check(query)
        if result.state in {"live", "no_results"} and result.expires_at:
            self.cache.put(
                cache_key,
                provider=provider.name,
                platform=query.platform,
                query=query.settings().model_dump(),
                result=result.to_cache(),
                checked_at=result.checked_at,
                expires_at=result.expires_at,
            )

        if result.state in {"live", "no_results"}:
            return self._result(
                result,
                query,
                dismissed_ids=dismissed_ids,
                weights=weights,
                state=None,
                cached=False,
                stale=False,
            )

        if cached:
            expires_at = _date(cached.get("expires_at"))
            stale_limit = expires_at + timedelta(seconds=self.settings.trend_stale_ttl_seconds) if expires_at else None
            if stale_limit is not None and now <= stale_limit:
                cached_result = TrendProviderResult.from_cache(cached.get("result") or {})
                return self._result(
                    cached_result,
                    query,
                    dismissed_ids=dismissed_ids,
                    weights=weights,
                    state="stale",
                    cached=True,
                    stale=True,
                    notes=[
                        "The live provider failed, so the last successful result is shown with its original timestamp.",
                        *result.notes,
                    ],
                    extra_limitations=[
                        "This is cached data, not a live check. Treat freshness as unknown until refreshed.",
                    ],
                    error_code=result.error_code,
                    error_message=result.error_message,
                )

        return self._result(
            result,
            query,
            dismissed_ids=dismissed_ids,
            weights=weights,
            state=None,
            cached=False,
            stale=False,
        )

    def _result(
        self,
        provider_result: TrendProviderResult,
        query: TrendQuery,
        *,
        dismissed_ids: set[str] | None,
        weights: dict[str, float] | None,
        state: str | None,
        cached: bool,
        stale: bool,
        notes: list[str] | None = None,
        extra_limitations: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> TrendCheckResult:
        results = rank_trends(
            provider_result.observations,
            query,
            weights=weights,
            dismissed_ids=dismissed_ids,
        )
        resolved_state = state or provider_result.state
        if resolved_state == "live" and not results:
            resolved_state = "no_results"
        limitations = list(provider_result.limitations)
        if extra_limitations:
            limitations.extend(extra_limitations)
        limitations.append(
            "Trend ranking is a transparent weighted fit score, not a prediction that the clip will perform."
        )
        return TrendCheckResult(
            status=resolved_state,  # type: ignore[arg-type]
            provider=provider_result.provider,
            platform=query.platform,
            checked_at=provider_result.checked_at,
            expires_at=provider_result.expires_at,
            cached=cached,
            stale=stale,
            settings=query.settings(),
            results=results,
            notes=list(dict.fromkeys([*(provider_result.notes or []), *(notes or [])])),
            limitations=list(dict.fromkeys(limitations)),
            error_code=error_code or provider_result.error_code,
            error_message=error_message or provider_result.error_message,
        )

    def with_manual_references(
        self,
        result: TrendCheckResult,
        manual: list[ManualTrend],
        query: TrendQuery,
        *,
        dismissed_ids: set[str] | None = None,
        weights: dict[str, float] | None = None,
    ) -> TrendCheckResult:
        references = [manual_recommendation(item, query) for item in manual]
        dismissed = dismissed_ids or set()
        visible_references = [item for item in references if item.id not in dismissed]
        # Provider results are the source of truth for provider findings. Manual
        # entries are rebuilt from the full manual list on every merge, so drop
        # older manual copies to avoid showing the same reference twice.
        provider_results = [item for item in result.results if not item.manual]
        combined = rank_trends(
            [*provider_results, *visible_references],
            query,
            weights=weights,
            dismissed_ids=dismissed,
        )
        notes = list(result.notes)
        if references:
            notes.append("Manual references are user-supplied context; no provider verified their metrics.")
        return result.model_copy(
            update={
                "results": combined,
                "manual_references": visible_references,
                "notes": list(dict.fromkeys(notes)),
            }
        )


def manual_recommendation(item: ManualTrend, query: TrendQuery) -> TrendRecommendation:
    now = query.checked_at.astimezone(timezone.utc)
    digest = hashlib.sha1(f"{item.name}|{item.source_url or ''}".encode("utf-8")).hexdigest()[:12]
    observed = item.observed_at or now.isoformat()
    return TrendRecommendation(
        id=f"manual:{digest}",
        name=item.name[:200],
        platform=item.platform or "unknown",
        provider="manual",
        region=item.region or query.region,
        category=item.category or query.niche,
        evidence=item.evidence or "User-supplied trend reference.",
        volume=None,
        ranking=None,
        growth=None,
        observed_at=observed,
        updated_at=observed,
        published_at=None,
        direction=item.direction,
        source_url=item.source_url,
        freshness="unknown",
        confidence="low",
        limitations=[
            "Manual reference; no provider verified search volume, ranking or growth.",
            "Check the source link yourself before using the trend.",
        ],
        rights="You are responsible for confirming the rights to any referenced content.",
        manual=True,
    )
