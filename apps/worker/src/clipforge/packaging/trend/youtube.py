"""YouTube Data API trend research.

Honesty boundary: the Data API returns related public videos, their cumulative
view counts and their publication dates. It does not expose a live "Shorts
trend" feed, acceleration or engagement rate. Results are labelled accordingly
and never converted into a growth percentage.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clipforge.packaging.models import TrendRecommendation
from clipforge.packaging.trend.base import TrendProviderResult, TrendQuery

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/youtube/v3"
FETCH_TIMEOUT_SECONDS = 12


class TrendApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _fetch_json(url: str, timeout_seconds: int = FETCH_TIMEOUT_SECONDS) -> dict:
    request = Request(url, headers={"User-Agent": "Clipforge/0.1"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        reason = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            error = body.get("error") or {}
            detail = error.get("message") or ""
            errors = error.get("errors") or []
            if errors:
                reason = str(errors[0].get("reason") or "")
        except Exception:  # noqa: BLE001 - the body is best-effort diagnostics
            detail = ""
        if reason in {"quotaExceeded", "dailyLimitExceeded"} or exc.code == 429 and "quota" in detail.lower():
            raise TrendApiError("quota_exhausted", "The YouTube Data API quota is exhausted.", status=exc.code) from exc
        if exc.code == 429:
            raise TrendApiError("rate_limited", "The YouTube Data API rate limit was reached.", status=exc.code) from exc
        if exc.code in {401, 403}:
            raise TrendApiError("auth_missing", "The YouTube Data API key is missing or not authorised.", status=exc.code) from exc
        raise TrendApiError(
            "unavailable",
            f"The YouTube Data API rejected the request ({exc.code}). {detail}".strip(),
            status=exc.code,
        ) from exc
    except URLError as exc:
        raise TrendApiError("unavailable", f"The YouTube Data API could not be reached ({exc.reason}).") from exc
    except TimeoutError as exc:
        raise TrendApiError("unavailable", "The YouTube Data API did not answer in time.") from exc


class YouTubeTrendProvider:
    name = "youtube_data_api"
    platform = "youtube"

    def __init__(
        self,
        api_key: str,
        *,
        cache_ttl_seconds: int = 21_600,
        timeout_seconds: int = FETCH_TIMEOUT_SECONDS,
        fetch: Callable[[str], dict] | None = None,
    ) -> None:
        self.api_key = api_key
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._fetch = fetch or (lambda url: _fetch_json(url, timeout_seconds))

    def status(self) -> dict:
        return {
            "provider": self.name,
            "platform": self.platform,
            "label": "YouTube Data API v3",
            "operational": bool(self.api_key),
            "requires_credentials": True,
            "credential_env": "CLIPFORGE_YOUTUBE_API_KEY",
            "manual_research": True,
            "detail": (
                "Official related-video popularity data is available. This is not a live Shorts trend feed."
                if self.api_key
                else "Add CLIPFORGE_YOUTUBE_API_KEY to enable related-video popularity research. "
                "Manual trend references still work."
            ),
            "capabilities": {
                "related_video_popularity": True,
                "cumulative_view_count": True,
                "published_at": True,
                "live_shorts_trend_feed": False,
                "growth_or_acceleration": False,
                "engagement_rate": False,
            },
        }

    def check(self, query: TrendQuery) -> TrendProviderResult:
        now = query.checked_at.astimezone(timezone.utc)
        checked_at = now.isoformat()
        expires_at = (now + timedelta(seconds=self.cache_ttl_seconds)).isoformat()
        if not self.api_key:
            return TrendProviderResult(
                state="auth_missing",
                provider=self.name,
                platform=self.platform,
                checked_at=checked_at,
                expires_at=expires_at,
                notes=["YouTube trend research needs an API key configured on the worker."],
                limitations=[
                    "No YouTube Data API key is configured.",
                    "No trend metrics are shown without an official response.",
                ],
                error_code="auth_missing",
                error_message="Set CLIPFORGE_YOUTUBE_API_KEY and restart the worker.",
            )
        text = query.query_text()
        if not text:
            return TrendProviderResult(
                state="no_results",
                provider=self.name,
                platform=self.platform,
                checked_at=checked_at,
                expires_at=expires_at,
                notes=["No topic, title or transcript text was available to search."],
            )
        try:
            results = self._search(query, text, now)
        except TrendApiError as exc:
            return TrendProviderResult(
                state=exc.code,
                provider=self.name,
                platform=self.platform,
                checked_at=checked_at,
                expires_at=expires_at,
                notes=[str(exc)],
                limitations=[
                    "No result is shown because the official provider did not return usable data.",
                    "Clipforge does not estimate missing trend numbers.",
                ],
                error_code=exc.code,
                error_message=str(exc),
            )
        state = "live" if results else "no_results"
        return TrendProviderResult(
            state=state,
            provider=self.name,
            platform=self.platform,
            checked_at=checked_at,
            expires_at=expires_at,
            observations=results,
            notes=[
                f"Search query: {text}",
                "Cumulative views measure popularity, not acceleration.",
            ],
            limitations=[
                "The YouTube Data API returns related public videos, not a live Shorts trend feed.",
                "No growth percentage, post count or engagement rate is inferred.",
                "Reusing a discovered video requires the rights holder's permission.",
            ],
        )

    def _search(self, query: TrendQuery, text: str, now: datetime) -> list[TrendRecommendation]:
        published_after = (now - timedelta(days=query.freshness_days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        params = {
            "part": "snippet",
            "type": "video",
            "order": "viewCount",
            "videoDuration": "short",
            "maxResults": min(25, max(1, query.limit)),
            "q": text,
            "publishedAfter": published_after,
            "regionCode": query.region.upper() if len(query.region) == 2 else "US",
            "relevanceLanguage": query.language.split("-")[0][:2] or "en",
            "key": self.api_key,
        }
        payload = self._fetch(f"{API_ROOT}/search?{urlencode(params)}")
        items = [item for item in payload.get("items") or [] if isinstance(item, dict)]
        ids = [str((item.get("id") or {}).get("videoId") or "") for item in items]
        ids = [value for value in ids if value]
        details: dict[str, dict] = {}
        if ids:
            try:
                detail_payload = self._fetch(
                    f"{API_ROOT}/videos?{urlencode({'part': 'snippet,statistics', 'id': ','.join(ids), 'key': self.api_key})}"
                )
                details = {
                    str(item.get("id")): item
                    for item in detail_payload.get("items") or []
                    if isinstance(item, dict) and item.get("id")
                }
            except TrendApiError as exc:
                logger.info("YouTube video statistics unavailable: %s", exc)
        observations: list[TrendRecommendation] = []
        for index, item in enumerate(items[: query.limit]):
            snippet = item.get("snippet") or {}
            video_id = str((item.get("id") or {}).get("videoId") or "")
            if not video_id:
                continue
            detail = details.get(video_id) or {}
            detail_snippet = detail.get("snippet") or snippet
            stats = detail.get("statistics") or {}
            view_text = str(stats.get("viewCount") or "")
            volume = int(view_text) if view_text.isdigit() else None
            published = str(detail_snippet.get("publishedAt") or snippet.get("publishedAt") or "") or None
            freshness = self._freshness(published, now, query.freshness_days)
            name = re.sub(r"\s+", " ", str(detail_snippet.get("title") or "")).strip() or "(untitled video)"
            observations.append(
                TrendRecommendation(
                    id=f"youtube:{video_id}",
                    name=name[:200],
                    platform="youtube",
                    provider=self.name,
                    region=query.region.upper(),
                    category=(query.niche or query.genre or "related")[:80],
                    evidence=(
                        f"Public YouTube video ranked {index + 1} by cumulative views for a related search. "
                        "Popularity only; acceleration is not available."
                    ),
                    volume=volume,
                    ranking=index + 1,
                    growth=None,
                    observed_at=now.isoformat(),
                    updated_at=now.isoformat(),
                    published_at=published,
                    direction="unknown",
                    source_url=f"https://www.youtube.com/watch?v={video_id}",
                    freshness=freshness,  # type: ignore[arg-type]
                    confidence="medium" if volume is not None and len(items) >= 3 else "low",
                    limitations=[
                        "Cumulative views are not a growth or acceleration metric.",
                        "This is a related public video, not a verified Shorts trend.",
                    ],
                    rights="Public metadata only; reuse of the video requires the rights holder's permission.",
                )
            )
        return observations

    @staticmethod
    def _freshness(published_at: str | None, now: datetime, freshness_days: int) -> str:
        if not published_at:
            return "unknown"
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            return "unknown"
        age_days = max(0.0, (now - published).total_seconds() / 86_400)
        if age_days <= freshness_days:
            return "fresh"
        if age_days <= freshness_days * 2:
            return "recent"
        return "stale"
