"""Context-aware packaging: briefs, director, research and edit preservation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.packaging.brief import build_content_brief
from clipforge.packaging.director import score_titles
from clipforge.packaging.generator import PackagingContext, clip_text, generate_metadata
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
)
from clipforge.packaging.research.service import KeywordResearchRegistry, KeywordResearchService
from clipforge.packaging.skill import ACCURACY_GATE, SKILL_ID, SKILL_VERSION, SYSTEM_PROMPT, skill_manifest

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def make_segments(sentences: list[str], *, first_start_ms: int = 400, sentence_ms: int = 5_000) -> list[dict]:
    segments: list[dict] = []
    cursor = first_start_ms
    for index, sentence in enumerate(sentences):
        tokens = sentence.split()
        step = sentence_ms / max(1, len(tokens))
        words = [
            {
                "start_ms": cursor + int(position * step),
                "end_ms": cursor + int((position + 1) * step) - 40,
                "word": f" {token}",
                "probability": 0.92,
            }
            for position, token in enumerate(tokens)
        ]
        segments.append(
            {
                "id": index,
                "start_ms": cursor,
                "end_ms": cursor + sentence_ms,
                "text": f" {sentence}",
                "words": words,
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
            }
        )
        cursor += sentence_ms + 300
    return segments


PODCAST = [
    "Welcome back to the show, today we have a wide-ranging conversation.",
    "First, a quick word from our sponsor about project management software.",
    "Now let us talk about fundraising, which we covered in detail last year.",
    "Then we will get into hiring, which is where most founders struggle.",
    "The biggest mistake founders make is hiring for the role they think they need.",
    "You should hire for the problem you actually have right now.",
    "We spent six months looking for a head of sales before we had a single customer.",
    "That was a very expensive way to learn a simple lesson.",
    "But this only works if you actually listen instead of pitching.",
    "After the break we will cover pricing and packaging for a completely different audience.",
    "Pricing is a separate topic and deserves its own episode.",
]


def build_brief(
    sentences: list[str],
    *,
    clip_start: int = 0,
    clip_end: int | None = None,
    source_title: str = "Founder lessons: hiring, fundraising and pricing",
    settings: dict | None = None,
) -> tuple[ContentBrief, dict, dict]:
    segments = make_segments(sentences)
    end_index = len(segments) - 1 if clip_end is None else clip_end
    candidate = {
        "id": "candidate-1",
        "start_ms": segments[clip_start]["start_ms"],
        "end_ms": segments[end_index]["end_ms"],
        "title": "Hiring mistake",
        "hook_detail": {"text": sentences[clip_start]},
    }
    transcript = {"id": "transcript-1", "segments": segments}
    brief = build_content_brief(
        transcript,
        candidate,
        source_title=source_title,
        settings=settings or {"audience": "first-time founders", "language": "en"},
        platform="youtube_shorts",
    )
    return brief, transcript, candidate


def metadata_for(
    brief: ContentBrief,
    transcript: dict,
    candidate: dict,
    *,
    platform: str = "youtube_shorts",
    settings: dict | None = None,
    research: KeywordResearchResult | None = None,
):
    context = PackagingContext(
        transcript=transcript,
        candidate=candidate,
        platform=platform,
        topic="startup hiring",
        subtopics=["founder mistakes"],
        genre="education",
        settings=settings or {"audience": "first-time founders", "language": "en"},
        now=NOW,
    )
    return generate_metadata(context, brief, research)


class FakeCache:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self.rows.get(key)

    def put(self, key: str, **kwargs) -> None:
        self.rows[key] = kwargs


class CountingProvider:
    name = "counting"

    def __init__(self, result: ResearchProviderResult | None = None) -> None:
        self.calls = 0
        self.result = result or ResearchProviderResult(
            state="live",
            provider=self.name,
            observations=[
                KeywordObservation(
                    id="counting:1",
                    term="founder hiring",
                    provider=self.name,
                    kind="suggestion",
                    region="US",
                    language="en",
                    retrieved_at=NOW.isoformat(),
                    evidence="suggestion only",
                )
            ],
            notes=["suggestion only"],
        )

    def status(self) -> KeywordResearchProviderStatus:
        return KeywordResearchProviderStatus(provider=self.name, label="Counting", operational=True)

    def research(self, query: ResearchQuery) -> ResearchProviderResult:
        self.calls += 1
        return self.result


# --- brief construction -----------------------------------------------------


def test_brief_selects_a_later_insight_instead_of_the_opening_line():
    # given: a slow opening and a strong insight much later
    sentences = [
        "So yeah, welcome to the video, I am glad you are here.",
        "Before we start, let me adjust the microphone and the lights.",
        "Anyway, today we are talking about a few different things.",
        "The biggest mistake founders make is hiring for the role they think they need.",
        "You should hire for the problem you actually have right now.",
        "We spent six months looking for a head of sales before our first customer.",
        "That was an expensive way to learn a simple lesson.",
        "Talk to twenty customers yourself before you hire anyone.",
        "This only works if you actually listen instead of pitching.",
    ]
    brief, transcript, candidate = build_brief(sentences)

    # when
    metadata = metadata_for(brief, transcript, candidate)
    clip = clip_text(transcript, candidate)

    # then
    assert brief.central_claim != clip.sentences[0].text
    assert brief.stronger_hook != clip.sentences[0].text
    assert metadata.titles
    assert metadata.titles[0].text.strip().lower() != clip.sentences[0].text.strip().lower()
    assert any(title.text != clip.sentences[0].text for title in metadata.titles)


def test_clip_subject_is_distinct_from_the_wider_video():
    # given: five topics, but the clip covers only the hiring section
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # then
    assert "hir" in (brief.clip_topic + brief.central_claim).lower()
    assert "pricing" not in brief.central_claim.lower()
    assert brief.clip_type in {
        "standalone_insight",
        "concept_explanation",
        "practical_tutorial",
        "personal_story",
    }
    clip_evidence = [item for item in brief.evidence if item.in_clip]
    context_evidence = [item for item in brief.evidence if not item.in_clip]
    assert clip_evidence and all(item.kind != "context" for item in clip_evidence)
    assert all(item.kind == "context" for item in context_evidence)
    assert brief.minor_topics or brief.wider_context


def test_long_transcripts_are_summarised_hierarchically_and_report_omissions():
    # given: a source far longer than a single prompt should receive
    sentences = [
        f"Section {index // 6 + 1} covers topic {index % 7} with a specific example and a number {index}."
        for index in range(120)
    ]
    sentences[100] = "The key lesson is that customer interviews beat assumptions every single time."
    brief, transcript, candidate = build_brief(sentences, clip_start=100, clip_end=104)

    # then
    assert len(brief.sections) > 8
    assert brief.omitted_sections >= 1
    assert brief.context_chars > 0
    assert len([item for item in brief.evidence if item.in_clip]) >= 4
    assert brief.clip_topic


def test_ambiguous_clip_is_labelled_rather_than_guessed():
    # given: the clip opens mid-reference and lacks its subject
    sentences = [
        "We had already spent months on the wrong approach.",
        "It was the same problem we talked about earlier, just worse.",
        "That is why the numbers never made sense.",
        "So we stopped and started again.",
    ]
    brief, _, _ = build_brief(sentences, clip_start=1, clip_end=3)

    # then
    assert brief.clip_type == "excerpt_needs_context"
    assert brief.confidence in {"low", "medium"}
    assert any("context" in value.lower() for value in brief.ambiguity)


# --- title director ---------------------------------------------------------


def test_title_candidates_are_diverse_scored_and_grounded():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)
    metadata = metadata_for(brief, transcript, candidate)

    # then
    titles = metadata.titles
    assert 4 <= len(titles) <= 8
    assert len({title.text.lower() for title in titles}) == len(titles)
    assert len({title.angle for title in titles}) >= 3
    assert all(title.scores is not None for title in titles)
    assert all(0.0 <= (title.scores.weighted if title.scores else 0) <= 1.0 for title in titles)
    assert sum(1 for title in titles if title.accepted) >= 3
    assert metadata.editorial_rationale
    assert any(title.evidence_ids for title in titles if title.accepted)


def test_accuracy_gate_rejects_invented_numbers_names_and_context_leakage():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)
    clip = clip_text(transcript, candidate)
    sentences = [{"text": item.text, "start_ms": item.start_ms, "end_ms": item.end_ms} for item in clip.sentences]
    context_text = brief.wider_context[0] if brief.wider_context else "Pricing packaging audience episode"

    # when
    scored = score_titles(
        [
            {"text": "37% of founders hire the wrong person", "angle": "result"},
            {"text": "The Berlin hiring method", "angle": "descriptive"},
            {"text": context_text, "angle": "search"},
            {"text": "Hire for the problem you actually have", "angle": "insight"},
        ],
        brief=brief,
        clip_full_text=clip.full_text,
        clip_sentences=sentences,
        platform="youtube_shorts",
        settings={"platform": "youtube_shorts"},
    )
    by_text = {item.text: item for item in scored}

    # then
    assert by_text["37% of founders hire the wrong person"].accepted is False
    assert "Number" in by_text["37% of founders hire the wrong person"].rejection_reason
    assert by_text["The Berlin hiring method"].accepted is False
    assert "Berlin" in by_text["The Berlin hiring method"].rejection_reason
    assert by_text["Hire for the problem you actually have"].accepted is True
    if context_text in by_text:
        assert by_text[context_text].accepted is False


def test_descriptions_explain_the_clip_instead_of_copying_the_opening_line():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)
    metadata = metadata_for(brief, transcript, candidate)
    clip = clip_text(transcript, candidate)

    # then
    first_line = clip.sentences[0].text.strip().lower()
    assert metadata.descriptions.short.strip().lower() != first_line
    assert metadata.descriptions.short != metadata.titles[0].text
    assert "main point" in metadata.descriptions.long.lower()
    assert metadata.thumbnail_text is not None
    assert metadata.thumbnail_text.text.lower() != metadata.titles[0].text.lower()
    assert metadata.validation.ok is True


def test_platform_adaptation_uses_different_limits_and_description_style():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    tiktok = metadata_for(brief, transcript, candidate, platform="tiktok")
    shorts = metadata_for(brief, transcript, candidate, platform="youtube_shorts")
    long_form = metadata_for(
        brief,
        transcript,
        candidate,
        platform="youtube_shorts",
        settings={"audience": "first-time founders", "description_style": "long_form"},
    )

    # then
    assert tiktok.descriptions.long_limit < shorts.descriptions.long_limit
    assert tiktok.caption.limit == 2_200
    assert shorts.caption.limit == 5_000
    assert long_form.descriptions.long_limit == shorts.descriptions.long_limit


def test_short_description_leads_with_a_standalone_claim():
    # given: the strongest supporting sentence starts with a reference ("this")
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    metadata = metadata_for(brief, transcript, candidate)

    # then
    claim_tail = brief.central_claim.rstrip(".").lower()
    assert claim_tail in metadata.descriptions.short.lower()
    assert "that this only works" not in metadata.descriptions.short.lower()


def test_thumbnail_text_stands_alone_within_the_platform_limit():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    metadata = metadata_for(brief, transcript, candidate)

    # then
    thumbnail = metadata.thumbnail_text
    assert thumbnail is not None
    assert len(thumbnail.text) <= thumbnail.limit
    assert len(thumbnail.text.split()) >= 2
    assert thumbnail.text.lower().split()[0] not in {"this", "that", "it", "these", "those", "they"}
    assert thumbnail.text.split()[-1].lower() not in {
        "the", "a", "an", "for", "to", "of", "you", "and", "with", "that",
    }


def test_keywords_are_search_phrases_not_sentences_or_fragments():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    metadata = metadata_for(brief, transcript, candidate)

    # then
    phrases = [keyword.phrase for keyword in metadata.keywords]
    assert phrases
    assert all(len(phrase.split()) <= 5 for phrase in phrases)
    assert all(phrase[-1] not in ".,;:!?" for phrase in phrases)
    assert all(not phrase.lower().endswith((" this", " that", " the", " of", " to")) for phrase in phrases)
    # A spoken sentence must never be offered as a keyword.
    assert not any(len(phrase.split()) > 6 for phrase in phrases)


def test_clip_topic_never_joins_unrelated_terms_into_a_phrase():
    # given: only two sentences, with no source title overlap
    sentences = [
        "It was the same problem we discussed earlier.",
        "That is why the numbers never made sense.",
    ]
    brief, _, _ = build_brief(sentences, clip_start=0, clip_end=1, source_title="Weekly show")

    # then
    assert brief.clip_topic
    assert len(brief.clip_topic.split()) <= 3
    assert brief.clip_topic.lower() not in {"earlier made sense", "discussed earlier made"}


def test_metadata_does_not_reuse_the_same_text_in_every_field():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)
    metadata = metadata_for(brief, transcript, candidate)

    # then
    values = {
        metadata.titles[0].text.strip().lower(),
        metadata.descriptions.short.strip().lower(),
        metadata.descriptions.long.strip().lower(),
        metadata.caption.text.strip().lower(),
        (metadata.thumbnail_text.text if metadata.thumbnail_text else "").strip().lower(),
    }
    assert len(values) == 5


# --- research ---------------------------------------------------------------


def make_service(settings, providers: list, enhanced: list | None = None) -> KeywordResearchService:
    registry = KeywordResearchRegistry(free=providers, enhanced=enhanced or [])
    return KeywordResearchService(cache=FakeCache(), registry=registry, settings=settings)


def test_research_off_is_disabled_and_never_calls_a_provider(settings):
    # given
    provider = CountingProvider()
    service = make_service(settings, [provider])
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    result = service.research(brief, mode="off")

    # then
    assert result.status == "disabled"
    assert result.observations == []
    assert provider.calls == 0


def test_provider_failure_does_not_block_research_or_packaging(settings):
    # given
    class FailingProvider(CountingProvider):
        def research(self, query: ResearchQuery):
            self.calls += 1
            raise RuntimeError("network down")

    provider = FailingProvider()
    service = make_service(settings, [provider])
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    result = service.research(brief, mode="free")

    # then
    assert result.status == "unavailable"
    assert result.observations == []
    assert any("failed" in note.lower() or "no metric" in note.lower() for note in result.limitations + result.notes)


def test_manual_terms_work_without_any_api_key(settings):
    # given
    service = make_service(settings, [ManualResearchProvider()])
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    result = service.research(brief, mode="free", manual_terms=["founder hiring mistakes"])

    # then
    assert result.status == "live"
    assert result.suggested_terms
    manual = [item for item in result.observations if item.manual]
    assert manual and manual[0].value is None
    assert any("no search volume" in value.lower() for value in result.limitations)


def test_unavailable_providers_report_manual_required_without_metrics(settings):
    # given
    service = make_service(
        settings,
        [YouTubeAutocompleteProvider(False), GoogleTrendsBridgeProvider("")],
        enhanced=[VidIQProvider(False)],
    )
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    result = service.research(brief, mode="enhanced")

    # then
    assert result.status in {"manual_required", "auth_missing", "no_results"}
    assert all(item.value is None for item in result.observations)
    assert any("relative" in value.lower() or "not a documented" in value.lower() for value in result.limitations)


def test_research_cache_prevents_repeated_provider_calls(settings):
    # given
    provider = CountingProvider()
    service = make_service(settings, [provider])
    brief, _, _ = build_brief(PODCAST, clip_start=4, clip_end=8)

    # when
    first = service.research(brief, mode="free")
    second = service.research(brief, mode="free")

    # then
    assert provider.calls == 1
    assert first.status == "live"
    assert second.status == "cached"


def test_research_terms_are_source_grounded_and_can_enrich_keywords():
    # given
    brief, transcript, candidate = build_brief(PODCAST, clip_start=4, clip_end=8)
    research = KeywordResearchResult(
        mode="free",
        status="live",
        retrieved_at=NOW.isoformat(),
        suggested_terms=["founder hiring"],
        observations=[
            KeywordObservation(
                id="manual:1",
                term="founder hiring",
                provider="manual",
                kind="manual",
                region="US",
                language="en",
                retrieved_at=NOW.isoformat(),
                evidence="manual term",
                manual=True,
            )
        ],
    )

    # when
    metadata = metadata_for(brief, transcript, candidate, research=research)

    # then
    assert any("founder hiring" in item.phrase.lower() for item in metadata.keywords)
    assert any("founderhiring" in item.tag.lower() for item in metadata.hashtags)
    assert all(not item.verified for item in metadata.keywords)


# --- skill ------------------------------------------------------------------


def test_packaging_skill_is_versioned_and_documents_the_safety_rules():
    manifest = skill_manifest()

    assert manifest["id"] == SKILL_ID
    assert manifest["version"] == SKILL_VERSION
    assert manifest["accuracy_gate"] == ACCURACY_GATE
    assert 0.25 <= manifest["rubric"]["accuracy"] <= 0.35
    assert "selected clip is the primary subject" in SYSTEM_PROMPT.lower()
    assert "wider context" in SYSTEM_PROMPT.lower()
    assert "never invent" in SYSTEM_PROMPT.lower()
    assert "not a prediction" in SYSTEM_PROMPT.lower() or "never claim" in SYSTEM_PROMPT.lower()


# --- API edit preservation --------------------------------------------------


def seed_candidate(repos, project: dict) -> str:
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
                "excerpt": (
                    "The biggest mistake founders make is hiring for the role they think they need. "
                    "You should hire for the problem you actually have right now. "
                    "Talk to twenty customers yourself and write down what they say."
                ),
            }
        ],
    )
    return "candidate-1"


def test_manual_title_edit_survives_full_regeneration(settings, project):
    # given
    app = create_app(settings)
    repos = app.state.clipforge.repos
    candidate_id = seed_candidate(repos, project)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}
    url = f"/api/projects/{project['id']}/candidates/{candidate_id}/packaging"
    first = client.post(url, headers=headers, json={"platform": "youtube_shorts", "use_llm": False})
    metadata = first.json()["packaging"]["metadata"]
    metadata["titles"][0]["text"] = "My approved hiring title"
    patched = client.patch(url, headers=headers, json={"metadata": metadata, "edited_fields": ["titles"]})
    saved = patched.json()["packaging"]["metadata"]

    # when: full regeneration with the user's current draft sent back
    regenerated = client.post(
        url,
        headers=headers,
        json={
            "platform": "youtube_shorts",
            "use_llm": False,
            "metadata": saved,
            "edited_fields": ["titles"],
        },
    )

    # then
    body = regenerated.json()["packaging"]
    assert body["metadata"]["titles"][0]["text"] == "My approved hiring title"
    assert "titles" in body["edited_fields"]


def test_field_regeneration_keeps_other_approved_fields(settings, project):
    # given
    app = create_app(settings)
    repos = app.state.clipforge.repos
    candidate_id = seed_candidate(repos, project)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}
    url = f"/api/projects/{project['id']}/candidates/{candidate_id}/packaging"
    first = client.post(url, headers=headers, json={"platform": "youtube_shorts", "use_llm": False})
    metadata = first.json()["packaging"]["metadata"]
    metadata["descriptions"]["short"] = "My approved short description."
    client.patch(url, headers=headers, json={"metadata": metadata})

    # when: regenerate only titles
    regenerated = client.post(
        url,
        headers=headers,
        json={
            "platform": "youtube_shorts",
            "use_llm": False,
            "fields": ["titles"],
            "metadata": metadata,
            "edited_fields": ["description"],
        },
    )

    # then
    body = regenerated.json()["packaging"]["metadata"]
    assert body["descriptions"]["short"] == "My approved short description."
    assert body["titles"]


def test_config_endpoint_reports_the_versioned_skill_and_research_providers(settings, project):
    # given
    app = create_app(settings)
    client = TestClient(app)
    headers = {"x-clipforge-token": settings.token}

    # when
    response = client.get(f"/api/projects/{project['id']}/packaging/config", headers=headers)

    # then
    assert response.status_code == 200
    body = response.json()
    assert body["skill"]["id"] == SKILL_ID
    assert body["skill"]["version"] == SKILL_VERSION
    assert body["title_weights"]["accuracy"] > 0
    providers = {item["provider"] for item in body["research"]["providers"]}
    assert {"manual", "youtube_autocomplete", "google_trends", "vidiq"} <= providers
    manual = next(item for item in body["research"]["providers"] if item["provider"] == "manual")
    assert manual["operational"] is True


# --- representative evaluation set -----------------------------------------


@pytest.mark.parametrize(
    "name,sentences,clip_start,clip_end",
    [
        ("slow opening", ["Thanks for watching.", "Quick microphone check.", "Here is the actual lesson.", "Hire for the problem you have.", "Talk to customers first."], 2, 4),
        ("multi-topic", PODCAST, 4, 8),
        ("tutorial", ["First, open the settings panel.", "Then choose the export preset.", "Next, check the caption alignment.", "Finally, render the clip."], 0, 3),
        ("story", ["I still remember the day we lost the deal.", "We had ignored the customer's warning signs.", "That mistake cost us six months of work.", "Now we always run a pilot first."], 0, 3),
        ("technical", ["The API returns a JSON object with a status field.", "If the status is 429, back off and retry.", "Never retry a 400 error without changing the request."], 0, 2),
        ("caveated", ["Cold email can work for enterprise sales.", "But it only works if the list is genuinely targeted.", "Volume alone will damage your domain reputation."], 0, 2),
        ("ambiguous", ["It was the same problem we discussed earlier.", "That is why the numbers never made sense."], 0, 1),
        ("misleading candidate", ["Our revenue grew after we changed the onboarding flow.", "We also hired two salespeople that month.", "So we cannot claim the flow caused it."], 0, 2),
    ],
)
def test_evaluation_cases_produce_grounded_metadata(name, sentences, clip_start, clip_end):
    # given
    brief, transcript, candidate = build_brief(sentences, clip_start=clip_start, clip_end=clip_end)

    # when
    metadata = metadata_for(brief, transcript, candidate)

    # then
    assert metadata.titles, name
    assert metadata.descriptions.short, name
    assert metadata.descriptions.long, name
    assert metadata.validation.ok, (name, metadata.validation.issues)
    assert any(title.accepted for title in metadata.titles), name
    assert metadata.titles[0].text.strip().lower() != clip_text(transcript, candidate).sentences[0].text.strip().lower(), name
    assert len({title.text.lower() for title in metadata.titles}) == len(metadata.titles), name
