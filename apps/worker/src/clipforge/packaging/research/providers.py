"""Free-first research provider adapters.

Each adapter is isolated, returns provenance with every observation, and
degrades to an honest unavailable state. No adapter invents search volume,
trend velocity, competitor performance or audience data.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clipforge.packaging.models import KeywordObservation, KeywordResearchProviderStatus
from clipforge.packaging.research.base import ResearchProviderResult, ResearchQuery

logger = logging.getLogger(__name__)

SUGGEST_ENDPOINT = "https://suggestqueries.google.com/complete/search"
DEFAULT_TIMEOUT_SECONDS = 8


class ResearchApiError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fetch_json(url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> object:
    request = Request(url, headers={"User-Agent": "Clipforge/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 429:
            raise ResearchApiError("rate_limited", "The suggestion service rate limit was reached.") from exc
        raise ResearchApiError("unavailable", f"The research endpoint rejected the request ({exc.code}).") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ResearchApiError("unavailable", "The research endpoint could not be reached.") from exc


class ManualResearchProvider:
    """Always available. User-supplied terms carry no provider metrics."""

    name = "manual"
    label = "Manual research"

    def status(self) -> KeywordResearchProviderStatus:
        return KeywordResearchProviderStatus(
            provider=self.name,
            label=self.label,
            operational=True,
            manual_research=True,
            detail="Add target terms or notes yourself. No volume, ranking or growth is inferred.",
            limitations=["Manual entries are context, not measured data."],
        )

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        now = query.retrieved_at.astimezone(timezone.utc).isoformat()
        observations = [
            KeywordObservation(
                id=f"manual:{index}:{term.lower()}",
                term=term[:120],
                provider=self.name,
                kind="manual",
                region=query.region,
                language=query.language,
                retrieved_at=now,
                freshness="unknown",
                evidence="Manual research term supplied by the user.",
                limitations=["No provider verified this term or any metric for it."],
                confidence="low",
                manual=True,
            )
            for index, term in enumerate(query.manual_terms, start=1)
            if term.strip()
        ]
        return ResearchProviderResult(
            state="live" if observations else "no_results",
            provider=self.name,
            observations=observations,
            notes=["Manual terms are used as wording ideas only."],
            limitations=["Manual research supplies no search volume or trend measurement."],
        )


class YouTubeAutocompleteProvider:
    """Opt-in YouTube search-suggestion adapter.

    This uses a public suggestion endpoint that is not a documented, versioned
    API. It is therefore disabled by default, cached, and never presented as
    search volume. Operators must enable it explicitly and confirm that their
    use complies with the applicable terms.
    """

    name = "youtube_autocomplete"
    label = "YouTube search suggestions"

    def __init__(self, enabled: bool, *, fetch: Callable[[str], object] | None = None) -> None:
        self.enabled = enabled
        self._fetch = fetch or (lambda url: _fetch_json(url))

    def status(self) -> KeywordResearchProviderStatus:
        detail = (
            "Opt-in: returns query wording suggestions only. It is not a documented API and supplies no volume."
            if self.enabled
            else "Disabled by default. Enable CLIPFORGE_YOUTUBE_SUGGEST_ENABLED only if your use complies with the applicable terms."
        )
        return KeywordResearchProviderStatus(
            provider=self.name,
            label=self.label,
            operational=self.enabled,
            requires_approval=True,
            manual_research=True,
            detail=detail,
            limitations=[
                "Not a documented, versioned API.",
                "Suggestions do not measure search volume, demand or trend velocity.",
            ],
        )

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        if not self.enabled:
            return ResearchProviderResult(
                state="manual_required",
                provider=self.name,
                notes=["YouTube search suggestions are disabled by default."],
                limitations=["No suggestion data is shown."],
                error_code="disabled",
                error_message="Set CLIPFORGE_YOUTUBE_SUGGEST_ENABLED=true to enable this opt-in adapter.",
            )
        now = query.retrieved_at.astimezone(timezone.utc).isoformat()
        observations: list[KeywordObservation] = []
        seen: set[str] = set()
        for term in query.terms[:3]:
            params = {"client": "firefox", "ds": "yt", "hl": query.language[:5], "q": term}
            try:
                payload = self._fetch(f"{SUGGEST_ENDPOINT}?{urlencode(params)}")
            except ResearchApiError as exc:
                return ResearchProviderResult(
                    state=exc.code,
                    provider=self.name,
                    observations=observations,
                    notes=[str(exc)],
                    limitations=["No suggestion data is shown for the failed request."],
                    error_code=exc.code,
                    error_message=str(exc),
                )
            suggestions = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            if not isinstance(suggestions, list):
                continue
            for suggestion in suggestions[: query.limit]:
                value = " ".join(str(suggestion).split())[:120]
                if not value or value.lower() in seen:
                    continue
                seen.add(value.lower())
                observations.append(
                    KeywordObservation(
                        id=f"youtube_suggest:{len(observations) + 1}",
                        term=value,
                        provider=self.name,
                        kind="suggestion",
                        region=query.region,
                        language=query.language,
                        retrieved_at=now,
                        freshness="unknown",
                        evidence=f"YouTube search suggestion for '{term}'.",
                        limitations=[
                            "A suggestion indicates wording, not measured search volume or demand.",
                            "The suggestion endpoint is not a documented API and may change or stop working.",
                        ],
                        confidence="low",
                    )
                )
        return ResearchProviderResult(
            state="live" if observations else "no_results",
            provider=self.name,
            observations=observations,
            notes=["Suggestions are query wording only; no volume or trend score is inferred."],
            limitations=["Opt-in, undocumented suggestion endpoint. Treat availability as best effort."],
        )


class GoogleTrendsBridgeProvider:
    """Adapter for an approved Google Trends-compatible endpoint.

    Google's official Trends API was alpha and application-gated when checked.
    Clipforge therefore does not scrape the consumer site. If an operator has an
    approved endpoint, it is queried through a narrow, documented shape; the
    response must supply relative interest explicitly and is never converted
    into search volume.
    """

    name = "google_trends"
    label = "Google Trends (approved bridge)"

    def __init__(
        self,
        endpoint: str,
        api_key: str = "",
        *,
        fetch: Callable[[str], object] | None = None,
    ) -> None:
        self.endpoint = endpoint.strip()
        self.api_key = api_key.strip()
        self._fetch = fetch or (lambda url: _fetch_json(url))

    def status(self) -> KeywordResearchProviderStatus:
        return KeywordResearchProviderStatus(
            provider=self.name,
            label=self.label,
            operational=bool(self.endpoint),
            requires_credentials=bool(self.endpoint and self.api_key),
            requires_approval=True,
            manual_research=True,
            detail=(
                "An approved Trends-compatible endpoint is configured. Values are relative interest, not volumes."
                if self.endpoint
                else "No approved Trends endpoint is configured. Google's official API is application-gated; "
                "consumer-site scraping is not used. Manual research still works."
            ),
            limitations=[
                "Relative interest is not an absolute search volume.",
                "Availability depends on an approved provider; Clipforge does not scrape Google Trends.",
            ],
        )

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        if not self.endpoint:
            return ResearchProviderResult(
                state="manual_required",
                provider=self.name,
                notes=["No approved Google Trends endpoint is configured."],
                limitations=["No trend metric is shown without an approved provider response."],
                error_code="auth_missing",
                error_message="Configure an approved Trends endpoint, or use manual research.",
            )
        now = query.retrieved_at.astimezone(timezone.utc).isoformat()
        observations: list[KeywordObservation] = []
        for term in query.terms[:3]:
            params = {"q": term, "geo": query.region.upper(), "hl": query.language[:5]}
            if self.api_key:
                params["key"] = self.api_key
            try:
                payload = self._fetch(f"{self.endpoint}?{urlencode(params)}")
            except ResearchApiError as exc:
                return ResearchProviderResult(
                    state=exc.code,
                    provider=self.name,
                    observations=observations,
                    notes=[str(exc)],
                    limitations=["No trend metric is shown for the failed request."],
                    error_code=exc.code,
                    error_message=str(exc),
                )
            entries = payload.get("terms") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries[: query.limit]:
                if not isinstance(entry, dict):
                    continue
                name = " ".join(str(entry.get("term") or "").split())[:120]
                interest = entry.get("relative_interest")
                try:
                    value = float(interest) if interest is not None else None
                except (TypeError, ValueError):
                    value = None
                if not name:
                    continue
                observations.append(
                    KeywordObservation(
                        id=f"google_trends:{len(observations) + 1}",
                        term=name,
                        provider=self.name,
                        kind="relative_interest",
                        value=max(0.0, min(100.0, value)) if value is not None else None,
                        unit="relative search interest (0-100)" if value is not None else "",
                        region=query.region.upper(),
                        language=query.language,
                        retrieved_at=now,
                        freshness=str(entry.get("freshness") or "unknown"),
                        source_url=str(entry.get("source_url") or "") or None,
                        evidence=str(entry.get("evidence") or f"Relative interest from an approved endpoint for '{term}'."),
                        limitations=[
                            "Relative interest is not an absolute volume and does not prove demand.",
                            *[str(value) for value in entry.get("limitations") or []][:3],
                        ],
                        confidence="medium" if value is not None else "low",
                    )
                )
        return ResearchProviderResult(
            state="live" if observations else "no_results",
            provider=self.name,
            observations=observations,
            notes=["Approved Trends-compatible endpoint returned relative interest only."],
            limitations=["Relative index values must not be presented as search counts."],
        )


class VidIQProvider:
    """Optional vidIQ enrichment.

    vidIQ publishes an MCP server for AI clients with OAuth or a read-only API
    key. No general public REST API suitable for the Clipforge backend was
    verified. This adapter therefore never fabricates a connection and never
    invents vidIQ metrics; findings can be added through manual research.
    """

    name = "vidiq"
    label = "vidIQ (optional)"

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def status(self) -> KeywordResearchProviderStatus:
        return KeywordResearchProviderStatus(
            provider=self.name,
            label=self.label,
            operational=False,
            requires_credentials=True,
            requires_approval=True,
            manual_research=True,
            detail=(
                "vidIQ's MCP server is an AI-client integration, not a verified backend REST API. "
                "Clipforge does not reuse a ChatGPT connection. Add vidIQ findings as manual terms instead."
                if self.enabled
                else "Not configured. Use manual research for vidIQ findings."
            ),
            limitations=[
                "No public vidIQ REST API suitable for this backend was verified.",
                "No vidIQ metric is displayed unless a verified provider response supplies it.",
            ],
        )

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        return ResearchProviderResult(
            state="manual_required",
            provider=self.name,
            notes=[
                "vidIQ enrichment is manual: copy the terms or findings you verified in vidIQ into Clipforge.",
            ],
            limitations=[
                "Clipforge does not claim a vidIQ API connection.",
                "No keyword volume, difficulty or breakout score is invented.",
            ],
            error_code="manual_required",
            error_message="Use manual research for vidIQ findings.",
        )


class YouTubePublicVideoResearch:
    """Official YouTube Data API related-video metadata through the existing provider."""

    name = "youtube_data_api"
    label = "YouTube Data API v3"

    def __init__(self, trend_provider) -> None:
        self.trend_provider = trend_provider

    def status(self) -> KeywordResearchProviderStatus:
        status = self.trend_provider.status()
        return KeywordResearchProviderStatus(
            provider=self.name,
            label=self.label,
            operational=bool(status.get("operational")),
            requires_credentials=True,
            manual_research=True,
            detail=str(status.get("detail") or ""),
            limitations=[
                "Returns public related-video metadata, not search volume.",
                "Cumulative views measure popularity, not acceleration.",
            ],
        )

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        from clipforge.packaging.trend.base import TrendQuery

        text = " ".join(query.terms[:3])[:240]
        if not text:
            return ResearchProviderResult(state="no_results", provider=self.name, notes=["No search terms."])
        trend_query = TrendQuery(
            topic=text,
            platform="youtube",
            region=query.region,
            language=query.language,
            limit=min(8, query.limit),
            now=query.now,
        )
        result = self.trend_provider.check(trend_query)
        now = query.retrieved_at.astimezone(timezone.utc).isoformat()
        observations = [
            KeywordObservation(
                id=f"youtube_video:{item.id}",
                term=item.name,
                provider=self.name,
                kind="public_video",
                value=float(item.volume) if item.volume is not None else None,
                unit="cumulative views" if item.volume is not None else "",
                region=query.region.upper(),
                language=query.language,
                retrieved_at=now,
                freshness=item.freshness,
                source_url=item.source_url,
                evidence=item.evidence,
                limitations=item.limitations[:3],
                confidence=item.confidence,
            )
            for item in result.observations[: query.limit]
        ]
        return ResearchProviderResult(
            state=result.state,
            provider=self.name,
            observations=observations,
            notes=result.notes,
            limitations=result.limitations,
            error_code=result.error_code,
            error_message=result.error_message,
        )


def research_timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
