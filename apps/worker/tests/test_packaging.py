"""AI clip packaging, grounded metadata and optional trend research."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.packaging.generator import (
    ClipSentence,
    ClipText,
    PackagingContext,
    _caption,
    _validate,
    clip_text,
    generate_metadata,
)
from clipforge.packaging.llm import generate_metadata_llm
from clipforge.packaging.models import (
    ClipMetadata,
    HashtagSuggestion,
    ManualTrend,
    PackagingDocument,
    TitleVariant,
    TrendRecommendation,
)
from clipforge.packaging.trend.base import TrendProviderResult, TrendQuery
from clipforge.packaging.trend.ranking import merged_weights, score_trend
from clipforge.packaging.trend.registry import TrendProviderRegistry, build_trend_provider_registry
from clipforge.packaging.trend.service import TrendService, manual_recommendation
from clipforge.packaging.trend.youtube import TrendApiError, YouTubeTrendProvider
from clipforge.profiles.spec import DEFAULT_PROFILE, resolve_settings

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def make_context(sample_segments: list[dict], **overrides) -> PackagingContext:
    transcript = {"id": "transcript-1", "segments": sample_segments}
    candidate = {
        "id": "candidate-1",
        "start_ms": sample_segments[0]["start_ms"],
        "end_ms": sample_segments[-1]["end_ms"],
        "title": "Hiring mistake",
        "hook_detail": {"text": sample_segments[0]["text"].strip()},
    }
    values = {
        "transcript": transcript,
        "candidate": candidate,
        "platform": "tiktok",
        "topic": "startup hiring",
        "subtopics": ["founder mistakes"],
        "genre": "education",
        "now": NOW,
    }
    values.update(overrides)
    return PackagingContext(**values)


def make_query(**overrides) -> TrendQuery:
    values = {
        "topic": "startup hiring",
        "title": "How to hire for the problem you have",
        "subtopics": ["founder mistakes"],
        "niche": "startup",
        "genre": "education",
        "platform": "youtube_shorts",
        "region": "US",
        "language": "en",
        "freshness_days": 30,
        "now": NOW,
    }
    values.update(overrides)
    return TrendQuery(**values)


def make_trend(name: str, **overrides) -> TrendRecommendation:
    values = {
        "id": f"trend:{name}",
        "name": name,
        "platform": "youtube",
        "provider": "fake",
        "region": "US",
        "category": "startup",
        "evidence": "Related public video popularity, not a Shorts trend feed.",
        "observed_at": NOW.isoformat(),
        "published_at": NOW.isoformat(),
        "direction": "rising",
        "source_url": "https://www.youtube.com/watch?v=example",
        "rights": "Public metadata only.",
    }
    values.update(overrides)
    return TrendRecommendation(**values)


class FakeCache:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get(self, cache_key: str) -> dict | None:
        return self.rows.get(cache_key)

    def put(self, cache_key: str, **kwargs) -> None:
        self.rows[cache_key] = kwargs


class FakeTrendProvider:
    name = "fake_trends"
    platform = "youtube"

    def __init__(self, result: TrendProviderResult, *, name: str = "fake_trends") -> None:
        self.result = result
        self.name = name
        self.calls = 0

    def status(self) -> dict:
        return {"provider": self.name, "platform": self.platform, "operational": True}

    def check(self, query: TrendQuery) -> TrendProviderResult:
        self.calls += 1
        return self.result


class FakeLlm:
    name = "fake-llm"
    model = "fake-model"

    def __init__(self, payload: dict | None = None, *, available: bool = True) -> None:
        self.payload = payload or {}
        self._available = available

    def available(self) -> tuple[bool, str | None]:
        if self._available:
            return True, None
        return False, "No LLM endpoint configured"

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        return self.payload


# --- deterministic metadata -------------------------------------------------


def test_metadata_is_built_from_the_final_clip_and_records_provenance(sample_segments):
    # given
    context = make_context(sample_segments)

    # when
    metadata = generate_metadata(context)

    # then
    clip = clip_text(context.transcript, context.candidate)
    assert metadata.titles[0].text.lower() in clip.full_text.lower()
    assert metadata.provenance.method == "deterministic"
    assert metadata.provenance.transcript_id == "transcript-1"
    assert metadata.provenance.source_start_ms == context.candidate["start_ms"]
    assert metadata.provenance.sentence_ids == clip.sentence_ids


def test_titles_respect_platform_limits_and_explain_their_basis(sample_segments):
    # given
    context = make_context(sample_segments)

    # when
    metadata = generate_metadata(context)

    # then
    assert metadata.titles
    assert all(title.within_limit for title in metadata.titles)
    assert all(title.reason for title in metadata.titles)
    assert metadata.titles[0].style == "quote"


def test_hashtags_are_small_relevant_and_deduplicated(sample_segments):
    # given
    context = make_context(sample_segments)

    # when
    metadata = generate_metadata(context)

    # then
    tags = [item.tag.lower() for item in metadata.hashtags]
    assert len(tags) <= 5
    assert len(tags) == len(set(tags))
    assert all(tag.startswith("#") and len(tag) > 3 for tag in tags)
    assert all(item.reason and 0.0 <= item.relevance <= 1.0 for item in metadata.hashtags)


def test_hashtag_style_controls_count_until_an_explicit_count_is_set(sample_segments):
    # given
    minimal = generate_metadata(
        make_context(sample_segments, settings={"hashtag_style": "minimal"})
    )
    descriptive = generate_metadata(
        make_context(sample_segments, settings={"hashtag_style": "descriptive"})
    )
    capped = generate_metadata(
        make_context(
            sample_segments,
            settings={"hashtag_style": "descriptive", "hashtag_count": 2},
        )
    )

    # then
    assert len(minimal.hashtags) <= 3
    assert len(descriptive.hashtags) >= len(minimal.hashtags)
    assert len(capped.hashtags) == 2


def test_keywords_are_phrases_not_hashtags_and_are_labelled_unverified(sample_segments):
    # given
    context = make_context(sample_segments)

    # when
    metadata = generate_metadata(context)

    # then
    assert metadata.keywords
    assert all(not item.phrase.startswith("#") for item in metadata.keywords)
    assert all(item.source == "ai_generated" and item.verified is False for item in metadata.keywords)
    assert all(len(item.phrase.split()) >= 2 for item in metadata.keywords)


def test_each_platform_profile_produces_within_limit_metadata(sample_segments):
    # given
    platforms = ("tiktok", "youtube_shorts", "instagram_reels", "cross_platform")

    for platform in platforms:
        # when
        metadata = generate_metadata(make_context(sample_segments, platform=platform))

        # then
        assert metadata.caption.within_limit, platform
        assert all(title.within_limit for title in metadata.titles), platform
        assert metadata.caption.platform == platform


def test_tiktok_caption_count_uses_utf16_code_units():
    # given
    text = ClipText(
        sentences=[ClipSentence(text="Fire 🔥", start_ms=0, end_ms=10, sentence_id=0)],
        full_text="Fire 🔥",
        source_start_ms=0,
        source_end_ms=10,
        timeline_applied=False,
        warnings=[],
        sentence_ids=[0],
    )

    # when
    caption = _caption("tiktok", text, "Fire 🔥", "topic", [], None)

    # then
    utf16_units = len(caption.text.encode("utf-16-le")) // 2
    assert caption.char_count == utf16_units
    assert caption.char_count > len(caption.text)


def test_validation_checks_hashtags_and_cta_for_restricted_terms(sample_segments):
    # given
    context = make_context(sample_segments, settings={"restricted_words": ["guaranteed"]})
    metadata = generate_metadata(context)
    metadata.hashtags.append(
        HashtagSuggestion(
            tag="#guaranteed",
            category="topic",
            reason="test mutation",
            provenance="profile",
        )
    )

    # when
    validation = _validate(
        metadata,
        context=context,
        text=clip_text(context.transcript, context.candidate),
        topic="startup hiring",
    )

    # then
    assert validation.ok is False
    assert any("guaranteed" in issue for issue in validation.issues)


def test_timeline_order_is_used_when_the_editorial_plan_is_applied(sample_segments):
    # given
    first, second = sample_segments[0], sample_segments[1]
    context = make_context(
        sample_segments,
        candidate={
            "id": "candidate-1",
            "start_ms": sample_segments[0]["start_ms"],
            "end_ms": sample_segments[-1]["end_ms"],
            "title": "Hiring mistake",
            "editorial": {
                "editorial": {"applied": True},
                "timeline": {
                    "valid": True,
                    "entries": [
                        {"source_start_ms": second["start_ms"], "source_end_ms": second["end_ms"]},
                        {"source_start_ms": first["start_ms"], "source_end_ms": first["end_ms"]},
                    ],
                },
            },
        },
    )

    # when
    text = clip_text(context.transcript, context.candidate)

    # then
    assert text.timeline_applied is True
    assert text.full_text.startswith("You should hire for the problem")


def test_unapplied_timeline_falls_back_chronologically_with_a_warning(sample_segments):
    # given
    context = make_context(
        sample_segments,
        candidate={
            "id": "candidate-1",
            "start_ms": sample_segments[0]["start_ms"],
            "end_ms": sample_segments[-1]["end_ms"],
            "title": "Hiring mistake",
            "editorial": {
                "editorial": {"applied": False},
                "timeline": {
                    "valid": True,
                    "entries": [
                        {"source_start_ms": sample_segments[1]["start_ms"], "source_end_ms": sample_segments[1]["end_ms"]}
                    ],
                },
            },
        },
    )

    # when
    metadata = generate_metadata(context)

    # then
    assert any("not applied" in warning for warning in metadata.validation.warnings)
    assert metadata.provenance.timeline_applied is False


def test_validation_rejects_an_invented_number_and_a_restricted_claim(sample_segments):
    # given
    context = make_context(
        sample_segments,
        settings={"restricted_claims": ["never fails"], "restricted_words": ["guaranteed"]},
    )
    metadata = generate_metadata(context)
    metadata.titles.append(
        TitleVariant(
            text="37% of founders fail and this never fails",
            style="result",
            reason="test mutation",
            chars=40,
            limit=100,
            within_limit=True,
        )
    )

    # when
    validation = _validate(
        metadata,
        context=context,
        text=clip_text(context.transcript, context.candidate),
        topic="startup hiring",
    )

    # then
    assert validation.ok is False
    assert any("37" in issue for issue in validation.issues)
    assert any("never fails" in issue for issue in validation.issues)


# --- optional LLM enrichment ------------------------------------------------


def test_llm_draft_with_an_invented_proper_noun_is_rejected(sample_segments):
    # given
    context = make_context(sample_segments)
    payload = {
        "titles": [{"text": "The Berlin hiring mistake", "style": "direct"}],
        "description_short": "The biggest mistake founders make is hiring for the role they think they need.",
        "description_long": "The biggest mistake founders make is hiring for the role they think they need.",
        "caption": "The biggest mistake founders make is hiring for the role they think they need.",
        "hashtags": [{"tag": "#hiring", "category": "topic"}],
        "keywords": ["startup hiring mistake"],
        "cta": "",
        "thumbnail_text": "Hiring mistake",
    }

    # when
    metadata, issues = generate_metadata_llm(context, FakeLlm(payload))

    # then
    assert metadata is None
    assert any("Berlin" in issue for issue in issues)


def test_llm_draft_with_an_invented_statistic_is_rejected(sample_segments):
    # given
    context = make_context(sample_segments)
    payload = {
        "titles": [{"text": "37% of founders hire wrong", "style": "result"}],
        "description_short": "The biggest mistake founders make is hiring for the role they think they need.",
        "description_long": "The biggest mistake founders make is hiring for the role they think they need.",
        "caption": "The biggest mistake founders make is hiring for the role they think they need.",
        "hashtags": [{"tag": "#hiring", "category": "topic"}],
        "keywords": ["startup hiring mistake"],
        "cta": "",
        "thumbnail_text": "Hiring mistake",
    }

    # when
    metadata, issues = generate_metadata_llm(context, FakeLlm(payload))

    # then
    assert metadata is None
    assert any("37" in issue for issue in issues)


def test_llm_draft_with_a_restricted_hashtag_is_rejected(sample_segments):
    # given
    context = make_context(sample_segments, settings={"restricted_words": ["guaranteed"]})
    payload = {
        "titles": [{"text": "Hire for the problem you have", "style": "direct"}],
        "description_short": "Hire for the problem you actually have right now.",
        "description_long": "Hire for the problem you actually have right now.",
        "caption": "Hire for the problem you actually have right now.",
        "hashtags": [{"tag": "#guaranteed", "category": "topic"}],
        "keywords": ["startup hiring mistake"],
        "cta": "",
        "thumbnail_text": "Hire for the problem",
    }

    # when
    metadata, issues = generate_metadata_llm(context, FakeLlm(payload))

    # then
    assert metadata is None
    assert any("guaranteed" in issue for issue in issues)


def test_llm_unavailable_returns_a_reason_not_an_exception(sample_segments):
    # given
    context = make_context(sample_segments)

    # when
    metadata, issues = generate_metadata_llm(context, FakeLlm(available=False))

    # then
    assert metadata is None
    assert issues == ["No LLM endpoint configured"]


def test_grounded_llm_draft_is_accepted_with_provenance(sample_segments):
    # given
    context = make_context(sample_segments)
    payload = {
        "titles": [{"text": "Hire for the problem you have", "style": "direct"}],
        "description_short": "Hire for the problem you actually have right now.",
        "description_long": "Hire for the problem you actually have right now. Talk to customers before you hire.",
        "caption": "Hire for the problem you actually have right now.",
        "hashtags": [{"tag": "#hiring", "category": "topic"}],
        "keywords": ["startup hiring mistake"],
        "cta": "",
        "thumbnail_text": "Hire for the problem",
    }

    # when
    metadata, issues = generate_metadata_llm(context, FakeLlm(payload))

    # then
    assert issues == []
    assert metadata is not None
    assert metadata.provenance.method == "llm"
    assert metadata.provenance.provider == "fake-llm"
    assert metadata.caption.text == payload["caption"]
    assert any(
        item.platform == "tiktok" and item.text == payload["caption"]
        for item in metadata.platform_captions
    )
    assert metadata.validation.ok is True


# --- trend ranking ----------------------------------------------------------


def test_ranking_with_empty_query_terms_uses_transcript_terms():
    # given
    query = make_query(
        topic="",
        niche="",
        title="",
        subtopics=[],
        transcript="startup hiring founders talk about mistakes",
    )

    # when
    scored = score_trend(make_trend("startup hiring mistakes", category="", evidence=""), query)

    # then
    assert scored.scores.semantic_relevance > 0
    assert scored.status in {"strong_match", "relevant_adjacent", "broad", "not_recommended"}


def test_ranked_trends_explain_status_and_do_not_fill_missing_metrics():
    # given
    query = make_query()
    strong = score_trend(
        make_trend("startup hiring founder mistakes", evidence="Founder mistakes in startup hiring."),
        query,
    )
    weak = score_trend(
        make_trend("cooking pasta recipes", category="cooking", evidence="Pasta recipes for weeknights."),
        query,
    )

    # when
    strong_status = strong.status
    weak_status = weak.status

    # then
    assert strong_status == "strong_match"
    assert strong.status_reason and strong.adaptation
    assert weak_status == "not_recommended"
    assert strong.volume is None
    assert strong.growth is None


def test_broad_but_weakly_supported_trend_is_labelled_broad():
    # given
    query = make_query()
    trend = make_trend(
        "startup",
        published_at=(NOW - timedelta(days=25)).isoformat(),
    )

    # when
    ranked = score_trend(trend, query)

    # then
    assert ranked.status in {"broad", "not_recommended"}
    assert ranked.scores.weighted < 0.52


def test_ranking_weights_are_normalised_and_overridable():
    # given
    custom = merged_weights({"semantic_relevance": 1.0})

    # when
    total = sum(custom.values())

    # then
    assert abs(total - 1.0) < 0.001
    assert custom["semantic_relevance"] > merged_weights()["semantic_relevance"]


# --- official YouTube provider ---------------------------------------------


def test_youtube_without_a_key_reports_auth_missing_honestly():
    # given
    provider = YouTubeTrendProvider("")

    # when
    result = provider.check(make_query())

    # then
    assert result.state == "auth_missing"
    assert result.error_code == "auth_missing"
    assert result.observations == []


def test_youtube_labels_popularity_without_inventing_growth():
    # given
    payloads = {
        "search?": {
            "items": [
                {"id": {"videoId": "a"}, "snippet": {"title": "Startup hiring", "publishedAt": NOW.isoformat()}},
                {"id": {"videoId": "b"}, "snippet": {"title": "Founder mistakes", "publishedAt": NOW.isoformat()}},
                {"id": {"videoId": "c"}, "snippet": {"title": "Hire slowly", "publishedAt": NOW.isoformat()}},
            ]
        },
        "videos?": {
            "items": [
                {"id": "a", "snippet": {"title": "Startup hiring", "publishedAt": NOW.isoformat()}, "statistics": {"viewCount": "1200"}},
                {"id": "b", "snippet": {"title": "Founder mistakes", "publishedAt": NOW.isoformat()}, "statistics": {"viewCount": "900"}},
                {"id": "c", "snippet": {"title": "Hire slowly", "publishedAt": NOW.isoformat()}, "statistics": {"viewCount": "300"}},
            ]
        },
    }
    provider = YouTubeTrendProvider("key", fetch=lambda url: payloads["search?"] if "/search?" in url else payloads["videos?"])

    # when
    result = provider.check(make_query())

    # then
    assert result.state == "live"
    assert result.observations[0].volume == 1200
    assert result.observations[0].ranking == 1
    assert result.observations[0].growth is None
    assert result.observations[0].direction == "unknown"
    assert any("not a live Shorts trend feed" in value for value in result.limitations)


def test_youtube_missing_statistics_leave_volume_empty():
    # given
    payloads = {
        "search?": {"items": [{"id": {"videoId": "a"}, "snippet": {"title": "Startup hiring", "publishedAt": NOW.isoformat()}}]},
        "videos?": {"items": [{"id": "a", "snippet": {"title": "Startup hiring", "publishedAt": NOW.isoformat()}}]},
    }
    provider = YouTubeTrendProvider("key", fetch=lambda url: payloads["search?"] if "/search?" in url else payloads["videos?"])

    # when
    result = provider.check(make_query())

    # then
    assert result.observations[0].volume is None
    assert result.observations[0].confidence == "low"


def test_youtube_quota_error_becomes_a_provider_state():
    # given
    def exhausted(url: str) -> dict:
        raise TrendApiError("quota_exhausted", "The YouTube Data API quota is exhausted.")

    provider = YouTubeTrendProvider("key", fetch=exhausted)

    # when
    result = provider.check(make_query())

    # then
    assert result.state == "quota_exhausted"
    assert result.observations == []


# --- cache, stale fallback and manual research ------------------------------


def live_provider_result() -> TrendProviderResult:
    return TrendProviderResult(
        state="live",
        provider=FakeTrendProvider.name,
        platform="youtube",
        checked_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(hours=1)).isoformat(),
        observations=[make_trend("startup hiring for founders")],
        limitations=["Related public videos, not a live Shorts trend feed."],
    )


def make_service(settings, provider: FakeTrendProvider, cache: FakeCache) -> TrendService:
    registry = TrendProviderRegistry({"youtube": provider, "youtube_shorts": provider, "cross_platform": provider})
    return TrendService(cache=cache, registry=registry, settings=settings)


def test_fresh_cache_is_served_without_a_second_provider_call(settings):
    # given
    provider = FakeTrendProvider(live_provider_result())
    service = make_service(settings, provider, FakeCache())

    # when
    first = service.check(make_query())
    second = service.check(make_query())

    # then
    assert first.status == "live"
    assert second.status == "cached"
    assert provider.calls == 1


def test_failed_provider_falls_back_to_stale_cache_with_its_original_timestamp(settings):
    # given
    cache = FakeCache()
    provider = FakeTrendProvider(live_provider_result())
    service = make_service(settings, provider, cache)
    service.check(make_query())
    provider.result = TrendProviderResult(
        state="unavailable",
        provider=FakeTrendProvider.name,
        platform="youtube",
        checked_at=(NOW + timedelta(hours=2)).isoformat(),
        error_code="unavailable",
        error_message="Provider offline",
    )

    # when
    stale = service.check(make_query(now=NOW + timedelta(hours=2)))

    # then
    assert stale.status == "stale"
    assert stale.cached is True
    assert stale.stale is True
    assert stale.checked_at == NOW.isoformat()
    assert any("cached data" in value for value in stale.limitations)


def test_a_failed_provider_without_cache_never_invents_results(settings):
    # given
    provider = FakeTrendProvider(
        TrendProviderResult(
            state="auth_missing",
            provider=FakeTrendProvider.name,
            platform="youtube",
            checked_at=NOW.isoformat(),
            error_code="auth_missing",
            error_message="Missing key",
        )
    )
    service = make_service(settings, provider, FakeCache())

    # when
    result = service.check(make_query())

    # then
    assert result.status == "auth_missing"
    assert result.results == []
    assert result.error_code == "auth_missing"


def test_manual_trend_reference_is_labelled_unverified():
    # given
    item = ManualTrend(
        name="Founder hiring",
        platform="tiktok",
        region="US",
        evidence="Seen on a creator account.",
        direction="rising",
    )

    # when
    reference = manual_recommendation(item, make_query(platform="tiktok"))

    # then
    assert reference.manual is True
    assert reference.volume is None
    assert reference.ranking is None
    assert reference.growth is None
    assert any("no provider verified" in value for value in reference.limitations)


def test_manual_reference_is_merged_into_results_without_erasing_provider_results(settings):
    # given
    provider = FakeTrendProvider(live_provider_result())
    service = make_service(settings, provider, FakeCache())
    result = service.check(make_query())
    manual = [ManualTrend(name="Startup hiring tips", platform="youtube", region="US")]

    # when
    merged = service.with_manual_references(result, manual, make_query())

    # then
    assert merged.manual_references[0].name == "Startup hiring tips"
    assert any(item.id.startswith("manual:") for item in merged.results)
    assert any(item.id == "trend:startup hiring for founders" for item in merged.results)


def test_merging_manual_references_twice_does_not_duplicate_them(settings):
    # given
    provider = FakeTrendProvider(live_provider_result())
    service = make_service(settings, provider, FakeCache())
    result = service.check(make_query())
    first_reference = ManualTrend(name="Startup hiring tips", platform="youtube", region="US")
    second_reference = ManualTrend(name="Founder interview clips", platform="youtube", region="US")
    first = service.with_manual_references(result, [first_reference], make_query())

    # when
    second = service.with_manual_references(
        first,
        [first_reference, second_reference],
        make_query(),
    )

    # then
    manual_ids = [item.id for item in second.results if item.manual]
    assert len(manual_ids) == 2
    assert len(manual_ids) == len(set(manual_ids))
    assert {item.name for item in second.manual_references} == {
        "Startup hiring tips",
        "Founder interview clips",
    }


def test_default_registry_reports_unavailable_platforms_and_missing_credentials(settings):
    # given
    registry = build_trend_provider_registry(settings)

    # when
    statuses = {item["platform"]: item for item in registry.statuses()}

    # then
    assert statuses["youtube"]["operational"] is False
    assert statuses["tiktok"]["manual_research"] is True
    assert statuses["instagram_reels"]["manual_research"] is True
    assert len(registry.statuses()) == len(statuses)


# --- profiles and persistence ----------------------------------------------


def test_profile_packaging_settings_resolve_with_project_override_precedence():
    # given
    profile = {
        "id": "profile-1",
        "name": "Founder brand",
        "version": 2,
        "document": {"packaging": {"tone": "warm", "hashtag_style": "minimal", "brand_terms": ["Acme"]}},
    }

    # when
    resolved = resolve_settings(profile, {"packaging": {"cta_preference": "none", "brand_terms": ["Override"]}})

    # then
    packaging = resolved["settings"]["packaging"]
    assert packaging["tone"] == "warm"
    assert packaging["hashtag_style"] == "minimal"
    assert packaging["cta_preference"] == "none"
    assert packaging["brand_terms"] == ["Override"]
    assert resolved["provenance"]["packaging.cta_preference"] == "project"


def test_old_profiles_still_resolve_new_packaging_defaults():
    # given
    profile = {"id": "old", "name": "Old", "version": 1, "document": {"export": {"format": "mp4"}}}

    # when
    resolved = resolve_settings(profile, None)

    # then
    assert resolved["settings"]["packaging"] == DEFAULT_PROFILE["packaging"]


def test_editing_packaging_metadata_does_not_touch_media_or_create_a_render(repos, project):
    # given
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [
            {
                "id": "candidate-1",
                "start_ms": 400,
                "end_ms": 8_000,
                "title": "Hiring mistake",
                "hook": "The biggest mistake founders make",
                "summary": "A grounded summary.",
            }
        ],
    )
    before = repos.candidates.get(project["id"], "candidate-1")
    document = PackagingDocument(
        platform="tiktok",
        settings={"tone": "warm"},
        metadata=ClipMetadata(
            titles=[TitleVariant(text="Hire for the problem", style="direct", reason="Edited by user.")]
        ),
    ).model_dump(mode="json")

    # when
    after = repos.candidates.set_packaging(project["id"], "candidate-1", document)

    # then
    assert (after["start_ms"], after["end_ms"]) == (before["start_ms"], before["end_ms"])
    assert after["packaging"]["metadata"]["titles"][0]["text"] == "Hire for the problem"
    assert repos.renders.list_for_project(project["id"]) == []


def test_trend_cache_is_provider_scoped(settings):
    # given
    cache = FakeCache()
    provider_one = FakeTrendProvider(live_provider_result(), name="provider_one")
    provider_two = FakeTrendProvider(live_provider_result(), name="provider_two")

    # when
    service_one = make_service(settings, provider_one, cache)
    service_two = make_service(settings, provider_two, cache)
    first = service_one.check(make_query())
    second = service_two.check(make_query())

    # then
    assert first.status == "live"
    assert second.status == "live"
    assert provider_one.calls == 1
    assert provider_two.calls == 1


# --- API wiring --------------------------------------------------------------


def seed_api_candidate(repos, project: dict) -> str:
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [
            {
                "id": "candidate-1",
                "start_ms": 400,
                "end_ms": 8_000,
                "title": "Hiring mistake",
                "hook": "The biggest mistake founders make",
                "excerpt": "The biggest mistake founders make is hiring for the role they think they need.",
            }
        ],
    )
    return "candidate-1"


def test_packaging_config_endpoint_reports_settings_and_provider_gaps(settings, project):
    # given
    app = create_app(settings)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}

    # when
    response = client.get(f"/api/projects/{project['id']}/packaging/config", headers=headers)

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["platforms"]["tiktok"]["caption_max"] == 2_200
    providers = {item["platform"]: item for item in body["providers"]["providers"]}
    assert providers["youtube"]["operational"] is False
    assert providers["tiktok"]["manual_research"] is True


def test_automatic_trend_preference_runs_a_check_without_an_explicit_flag(settings, project):
    # given
    app = create_app(settings)
    repos = app.state.clipforge.repos
    candidate_id = seed_api_candidate(repos, project)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}
    url = f"/api/projects/{project['id']}/candidates/{candidate_id}/packaging"

    # when
    generated = client.post(
        url,
        headers=headers,
        json={
            "platform": "tiktok",
            "topic": "startup hiring",
            "use_llm": False,
            "settings": {"trend_check": "automatic"},
        },
    )

    # then
    assert generated.status_code == 200
    trend = generated.json()["packaging"]["trend"]
    assert trend["status"] == "manual_required"
    assert trend["provider"] == "tiktok_official"


def test_packaging_endpoints_generate_and_edit_without_creating_a_render(settings, project):
    # given
    app = create_app(settings)
    repos = app.state.clipforge.repos
    candidate_id = seed_api_candidate(repos, project)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}
    url = f"/api/projects/{project['id']}/candidates/{candidate_id}/packaging"

    # when
    generated = client.post(
        url,
        headers=headers,
        json={
            "platform": "tiktok",
            "topic": "startup hiring",
            "subtopics": ["founder mistakes"],
            "use_llm": False,
            "check_trend": False,
        },
    )
    body = generated.json()
    metadata = body["packaging"]["metadata"]
    metadata["titles"][0]["text"] = "Hire for the problem you have"
    patched = client.patch(url, headers=headers, json={"metadata": metadata})

    # then
    assert generated.status_code == 200
    assert metadata["titles"][0]["limit"] == 100
    assert patched.status_code == 200
    saved = patched.json()
    assert saved["candidate"]["packaging"]["metadata"]["titles"][0]["text"] == "Hire for the problem you have"
    assert (saved["candidate"]["start_ms"], saved["candidate"]["end_ms"]) == (400, 8_000)
    assert repos.renders.list_for_project(project["id"]) == []
