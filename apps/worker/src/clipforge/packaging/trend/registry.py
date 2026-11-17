"""Provider registry and capability reporting."""

from __future__ import annotations

from dataclasses import dataclass, field

from clipforge.config import Settings
from clipforge.packaging.trend.base import (
    TrendProvider,
    TrendProviderResult,
    TrendQuery,
    UnavailableTrendProvider,
)
from clipforge.packaging.trend.youtube import YouTubeTrendProvider


@dataclass
class CrossPlatformTrendProvider:
    """Cross-platform checks use only providers that are actually available."""

    youtube: TrendProvider
    unavailable: list[TrendProvider] = field(default_factory=list)
    name: str = "cross_platform"
    platform: str = "cross_platform"

    def status(self) -> dict:
        youtube_status = self.youtube.status()
        return {
            "provider": self.name,
            "platform": self.platform,
            "label": "Cross-platform research",
            "operational": bool(youtube_status.get("operational")),
            "requires_credentials": True,
            "manual_research": True,
            "detail": (
                "Uses official YouTube related-video data. TikTok and Instagram require manual references "
                "or approved official access."
            ),
            "capabilities": youtube_status.get("capabilities", {}),
            "components": [youtube_status, *(provider.status() for provider in self.unavailable)],
        }

    def check(self, query: TrendQuery) -> TrendProviderResult:
        status = self.youtube.status()
        if not status.get("operational"):
            return TrendProviderResult(
                state="manual_required",
                provider=self.name,
                platform=self.platform,
                checked_at=query.checked_at.isoformat(),
                notes=[
                    "No official cross-platform trend provider is configured.",
                    "Use manual references or configure an approved provider.",
                ],
                limitations=[
                    "TikTok and Instagram have no configured official trend access.",
                    "No cross-platform trend metric is shown without provider evidence.",
                ],
                error_code="manual_research_required",
                error_message="Configure an official provider or add manual references.",
            )
        result = self.youtube.check(query)
        return TrendProviderResult(
            state=result.state,
            provider=self.name,
            platform=self.platform,
            checked_at=result.checked_at,
            expires_at=result.expires_at,
            observations=result.observations,
            notes=[
                *result.notes,
                "Cross-platform check used YouTube only; TikTok and Instagram results require manual research.",
            ],
            limitations=[
                *result.limitations,
                "This is not a combined cross-platform trend index.",
            ],
            error_code=result.error_code,
            error_message=result.error_message,
        )


class TrendProviderRegistry:
    def __init__(self, providers: dict[str, TrendProvider]) -> None:
        self._providers = providers

    def get(self, platform: str) -> TrendProvider:
        return self._providers.get(platform) or self._providers["cross_platform"]

    def statuses(self) -> list[dict]:
        statuses: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for provider in self._providers.values():
            status = provider.status()
            key = (str(status.get("provider")), str(status.get("platform")))
            if key in seen:
                continue
            seen.add(key)
            statuses.append(status)
        return statuses

    def config(self) -> dict:
        return {
            "providers": self.statuses(),
            "note": (
                "Only official or user-supplied sources are used. Missing credentials and approvals "
                "are reported as unavailable rather than filled with invented numbers."
            ),
        }


def build_trend_provider_registry(settings: Settings) -> TrendProviderRegistry:
    youtube = YouTubeTrendProvider(
        settings.youtube_api_key,
        cache_ttl_seconds=settings.trend_cache_ttl_seconds,
        timeout_seconds=settings.trend_timeout_seconds,
    )
    tiktok = UnavailableTrendProvider(
        name="tiktok_official",
        platform="tiktok",
        label="TikTok official trends",
        detail=(
            "TikTok trend access requires an approved official API or Research Tools entitlement. "
            "No restricted scraping is used; add manual trend references instead."
        ),
    )
    instagram = UnavailableTrendProvider(
        name="instagram_official",
        platform="instagram_reels",
        label="Instagram official trends",
        detail=(
            "Meta's official publishing APIs expose media publishing, not a general Reels trend feed. "
            "Add manual trend references or an approved provider."
        ),
    )
    providers: dict[str, TrendProvider] = {
        "youtube": youtube,
        "youtube_shorts": youtube,
        "tiktok": tiktok,
        "instagram_reels": instagram,
        "cross_platform": CrossPlatformTrendProvider(youtube=youtube, unavailable=[tiktok, instagram]),
    }
    return TrendProviderRegistry(providers)
