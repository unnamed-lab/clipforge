"""Transparent trend ranking.

Every positive and negative component is stored on the recommendation so the UI
can explain a status. Missing provider metrics stay missing: the ranker never
fills a volume, growth percentage or post count.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from clipforge.packaging.models import TrendRecommendation, TrendScoreBreakdown
from clipforge.packaging.trend.base import TrendQuery

DEFAULT_TREND_WEIGHTS: dict[str, float] = {
    "semantic_relevance": 0.26,
    "platform_relevance": 0.12,
    "regional_relevance": 0.10,
    "recency": 0.12,
    "rising_interest": 0.14,
    "content_suitability": 0.14,
    "misleading_risk": 0.07,
    "rights_constraint": 0.05,
}

STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "you", "our", "about", "what",
    "when", "where", "why", "how", "into", "over", "after", "before", "they", "them", "there",
    "here", "have", "has", "was", "were", "are", "is", "it", "its", "to", "of", "on", "in", "a",
    "an", "as", "at", "be", "by", "or", "we", "i", "me", "my", "us", "not", "no", "so", "if",
}


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", value or "")
        if token.lower() not in STOPWORDS
    }


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _recency(trend: TrendRecommendation, query: TrendQuery, now: datetime) -> float:
    observed = _date(trend.published_at) or _date(trend.observed_at)
    if observed is None:
        return 0.4
    age_days = max(0.0, (now - observed).total_seconds() / 86_400)
    window = max(1, query.freshness_days)
    return _clamp(1.0 - (age_days / window))


def _semantic(trend: TrendRecommendation, query: TrendQuery) -> float:
    query_terms = _tokens(" ".join([query.topic, query.niche, query.title, *query.subtopics]))
    if not query_terms:
        query_terms = set(sorted(_tokens(query.transcript))[:12])
    trend_terms = _tokens(" ".join([trend.name, trend.category, trend.evidence]))
    if not query_terms or not trend_terms:
        return 0.2
    overlap = len(query_terms & trend_terms)
    return _clamp(overlap / max(1, min(6, len(query_terms))))


def _platform(trend: TrendRecommendation, query: TrendQuery) -> float:
    if query.platform == "cross_platform":
        return 0.8 if trend.platform in {"tiktok", "youtube", "instagram"} else 0.5
    if trend.platform == query.platform:
        return 1.0
    if {trend.platform, query.platform} <= {"youtube", "youtube_shorts"}:
        return 0.8
    return 0.2


def _regional(trend: TrendRecommendation, query: TrendQuery) -> float:
    if not query.region or not trend.region:
        return 0.5
    return 1.0 if trend.region.upper() == query.region.upper() else 0.25


def _rising(trend: TrendRecommendation) -> float:
    if trend.growth is not None:
        return _clamp(0.5 + (trend.growth / 200.0))
    return {"rising": 1.0, "flat": 0.5, "falling": 0.1, "unknown": 0.35}.get(trend.direction, 0.35)


def _suitability(trend: TrendRecommendation, query: TrendQuery) -> float:
    score = 0.4
    if _platform(trend, query) >= 0.8:
        score += 0.2
    if _semantic(trend, query) >= 0.35:
        score += 0.2
    if trend.category and query.niche and query.niche.lower() in trend.category.lower():
        score += 0.15
    return _clamp(score)


def _misleading_risk(trend: TrendRecommendation) -> float:
    risk = 0.1
    if trend.direction == "unknown":
        risk += 0.1
    if not trend.source_url and not trend.manual:
        risk += 0.25
    if re.search(r"\b(shocking|guaranteed|viral|secret|insane)\b", trend.name, re.IGNORECASE):
        risk += 0.35
    if "not a" in trend.evidence.lower() or "popularity" in trend.evidence.lower():
        risk -= 0.05
    return _clamp(risk)


def _rights_constraint(trend: TrendRecommendation) -> float:
    value = (trend.rights or "").lower()
    if trend.manual and not trend.source_url:
        return 0.6
    if any(word in value for word in ("permission", "licence", "license", "rights holder")):
        return 0.8
    return 0.3


def merged_weights(weights: dict[str, float] | None = None) -> dict[str, float]:
    merged = dict(DEFAULT_TREND_WEIGHTS)
    for key, value in (weights or {}).items():
        if key in merged:
            try:
                merged[key] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
    total = sum(merged.values())
    if total <= 0:
        return dict(DEFAULT_TREND_WEIGHTS)
    # Keep the documented ratios while making a custom weight set sum to 1.
    return {key: round(value / total, 4) for key, value in merged.items()}


def score_trend(
    trend: TrendRecommendation,
    query: TrendQuery,
    weights: dict[str, float] | None = None,
) -> TrendRecommendation:
    resolved = merged_weights(weights)
    now = query.checked_at.astimezone(timezone.utc)
    breakdown = TrendScoreBreakdown(
        semantic_relevance=round(_semantic(trend, query), 4),
        platform_relevance=round(_platform(trend, query), 4),
        regional_relevance=round(_regional(trend, query), 4),
        recency=round(_recency(trend, query, now), 4),
        rising_interest=round(_rising(trend), 4),
        content_suitability=round(_suitability(trend, query), 4),
        misleading_risk=round(_misleading_risk(trend), 4),
        rights_constraint=round(_rights_constraint(trend), 4),
    )
    weighted = (
        breakdown.semantic_relevance * resolved["semantic_relevance"]
        + breakdown.platform_relevance * resolved["platform_relevance"]
        + breakdown.regional_relevance * resolved["regional_relevance"]
        + breakdown.recency * resolved["recency"]
        + breakdown.rising_interest * resolved["rising_interest"]
        + breakdown.content_suitability * resolved["content_suitability"]
        - breakdown.misleading_risk * resolved["misleading_risk"]
        - breakdown.rights_constraint * resolved["rights_constraint"]
    )
    breakdown.weighted = round(max(0.0, min(1.0, weighted)), 4)
    status, reason = _status(breakdown, trend, query)
    return trend.model_copy(
        update={
            "scores": breakdown,
            "status": status,
            "status_reason": reason,
            "adaptation": _adaptation(status, trend, query),
        }
    )


def _status(
    breakdown: TrendScoreBreakdown,
    trend: TrendRecommendation,
    query: TrendQuery,
) -> tuple[str, str]:
    if breakdown.semantic_relevance < 0.15:
        return (
            "not_recommended",
            "The provider result has little semantic overlap with this clip's topic; using it would need a forced angle.",
        )
    if breakdown.weighted >= 0.68:
        return (
            "strong_match",
            "Strong topic and format overlap with fresh, relevant provider evidence.",
        )
    if breakdown.weighted >= 0.52:
        return (
            "relevant_adjacent",
            "Related to the clip topic, but the connection needs a specific, honest angle.",
        )
    if breakdown.weighted >= 0.34:
        return (
            "broad",
            "A broad-category signal; it should not be presented as a specific trend for this clip.",
        )
    return (
        "not_recommended",
        "Weak overlap, stale evidence or rights risk makes this a poor fit.",
    )


def _adaptation(status: str, trend: TrendRecommendation, query: TrendQuery) -> str:
    topic = query.topic or query.niche or "this topic"
    if status == "strong_match":
        return f"Use the {topic} angle with your own evidence and wording; do not copy the source video."
    if status == "relevant_adjacent":
        return f"Build an honest bridge from {topic} to the adjacent idea; say why the connection is relevant."
    if status == "broad":
        return "Use only as broad context, not as proof that this clip is on-trend."
    return "Do not force this trend into the clip; keep the factual packaging you already have."


def rank_trends(
    observations: list[TrendRecommendation],
    query: TrendQuery,
    *,
    weights: dict[str, float] | None = None,
    dismissed_ids: set[str] | None = None,
) -> list[TrendRecommendation]:
    dismissed = dismissed_ids or set()
    scored = [
        score_trend(trend, query, weights)
        for trend in observations
        if trend.id not in dismissed
    ]
    scored.sort(key=lambda item: (-item.scores.weighted, item.name.lower()))
    return scored
