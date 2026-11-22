"""The editorial director: evidence grounding, fallback and provider policy."""

from __future__ import annotations

from dataclasses import replace

from clipforge.editorial.director import (
    DirectorDecision,
    PlannedClip,
    PlannedSegment,
    _validate_against_candidates,
    run_director,
)
from clipforge.providers.llm.registry import LlmProviderRegistry, ProviderSelection, is_loopback
from clipforge.providers.llm.openai_compat import OpenAiCompatibleScorer


def _candidate(
    identifier: str,
    *,
    start: int,
    end: int,
    overall: float,
    edited: bool = False,
    timeline: dict | None = None,
) -> dict:
    return {
        "id": identifier,
        "start_ms": start,
        "end_ms": end,
        "title": identifier,
        "summary": "",
        "excerpt": "This is the spoken moment.",
        "overall": overall,
        "warnings": [],
        "edited": edited,
        "decision": "proposed",
        "editorial": timeline or {},
        "hook_detail": {},
    }


def _transcript() -> dict:
    return {
        "language": "en",
        "segments": [
            {
                "start_ms": 0,
                "end_ms": 4_000,
                "text": " This is the spoken moment.",
                "words": [
                    {"word": " This", "start_ms": 0, "end_ms": 400},
                    {"word": " is", "start_ms": 400, "end_ms": 700},
                    {"word": " the", "start_ms": 700, "end_ms": 1_000},
                    {"word": " spoken", "start_ms": 1_000, "end_ms": 1_600},
                    {"word": " moment", "start_ms": 1_600, "end_ms": 2_400},
                ],
            },
            {
                "start_ms": 4_500,
                "end_ms": 9_000,
                "text": " Then there is a second moment.",
                "words": [],
            },
        ],
    }


class FakeAdapter:
    def __init__(self, payloads=None, error: Exception | None = None) -> None:
        self.payloads = list(payloads or [])
        self.error = error
        self.calls = 0

    def complete_json_meta(self, system, user, *, max_tokens=None, max_attempts=2):
        self.calls += 1
        if self.error is not None:
            raise self.error
        payload = self.payloads.pop(0) if self.payloads else {"selected": []}
        return payload, {"total_tokens": 10, "attempts": 1}


class FakeRegistry:
    def __init__(self, adapter, *, privacy="local") -> None:
        self.adapter = adapter
        self.info = {
            "provider": "ollama",
            "model": "test-model",
            "privacy": privacy,
            "configured": True,
        }

    def select(self) -> ProviderSelection:
        return ProviderSelection(self.adapter, self.info, True, None)


def test_no_provider_uses_deterministic_selection(settings, repos, project):
    # given
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("a", start=0, end=4_000, overall=80), _candidate("b", start=4_500, end=9_000, overall=50)],
    )
    candidates = repos.candidates.list_for_project(project["id"])

    # when
    decision = run_director(
        repos,
        settings,
        None,
        project=project,
        source={"duration_ms": 10_000, "has_video": True, "has_audio": True},
        candidates=candidates,
        transcript=_transcript(),
        resolved_settings={"captions": {}, "export": {}, "brief": {}, "rules": {}},
        request={"clip_count": 1},
    )

    # then
    assert decision["mode"] == "deterministic_fallback"
    assert [clip["candidate_id"] for clip in decision["selected"]] == ["a"]
    assert decision["selected"][0]["segments"][0]["start_ms"] == 0
    assert decision["metrics"].get("model_calls") == 0


def test_manual_trim_is_preserved_as_one_chronological_segment(repos, project):
    # given a manually edited candidate
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("edited", start=1_000, end=6_000, overall=90, edited=True)],
    )
    candidates = repos.candidates.list_for_project(project["id"])

    # when
    from clipforge.editorial.director import _fallback_clip

    clip = _fallback_clip(candidates[0])

    # then
    assert clip.strategy == "chronological"
    assert [(segment.start_ms, segment.end_ms, segment.role) for segment in clip.segments] == [
        (1_000, 6_000, "main")
    ]


def test_model_plan_rejects_out_of_range_spans_and_invents_no_hook():
    # given a plan that points outside the candidate and invents a quote
    decision = DirectorDecision(
        selected=[
            PlannedClip(
                candidate_id="a",
                strategy="chronological",
                segments=[PlannedSegment(start_ms=0, end_ms=3_000)],
                hook={"text": "a quote that was never spoken", "start_ms": 0, "end_ms": 1_000},
            )
        ]
    )
    candidates = {"a": {"id": "a", "start_ms": 0, "end_ms": 4_000, "edited": False}}

    # when
    validated = _validate_against_candidates(
        decision,
        candidates,
        _transcript()["segments"],
        source_duration_ms=10_000,
        limit=5,
    )

    # then
    assert len(validated.selected) == 1
    assert validated.selected[0].hook is None
    assert any("hook" in risk.lower() for risk in validated.selected[0].risks)


def test_out_of_range_plan_is_dropped():
    # given
    decision = DirectorDecision(
        selected=[
            PlannedClip(
                candidate_id="a",
                segments=[PlannedSegment(start_ms=0, end_ms=8_000)],
            )
        ]
    )
    candidates = {"a": {"id": "a", "start_ms": 0, "end_ms": 4_000, "edited": False}}

    # when
    validated = _validate_against_candidates(
        decision,
        candidates,
        _transcript()["segments"],
        source_duration_ms=10_000,
        limit=5,
    )

    # then
    assert validated.selected == []
    assert validated.rejected


def test_malformed_model_output_falls_back_instead_of_claiming_ai(settings, repos, project):
    # given a provider that fails
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("a", start=0, end=4_000, overall=80)],
    )
    candidates = repos.candidates.list_for_project(project["id"])
    configured = replace(settings, llm_editorial_enabled=True)
    registry = FakeRegistry(FakeAdapter(error=ValueError("malformed JSON")))

    # when
    decision = run_director(
        repos,
        configured,
        registry,
        project=project,
        source={"duration_ms": 10_000, "has_video": True, "has_audio": True},
        candidates=candidates,
        transcript=_transcript(),
        resolved_settings={"captions": {}, "export": {}, "brief": {}, "rules": {}},
        request={"clip_count": 1},
    )

    # then
    assert decision["mode"] == "deterministic_fallback"
    assert decision["selected"]
    assert "could not produce a grounded plan" in decision["notes"]


def test_ungrounded_model_plan_is_retried_then_replaced_by_the_fallback(settings, repos, project):
    # given two answers that point outside the source evidence
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("a", start=0, end=4_000, overall=80)],
    )
    candidates = repos.candidates.list_for_project(project["id"])
    configured = replace(settings, llm_editorial_enabled=True)
    bad = {
        "selected": [
            {
                "candidate_id": "a",
                "strategy": "chronological",
                "segments": [{"start_ms": 0, "end_ms": 9_000, "role": "main"}],
            }
        ]
    }
    registry = FakeRegistry(FakeAdapter(payloads=[bad, bad]))

    # when
    decision = run_director(
        repos,
        configured,
        registry,
        project=project,
        source={"duration_ms": 10_000, "has_video": True, "has_audio": True},
        candidates=candidates,
        transcript=_transcript(),
        resolved_settings={"captions": {}, "export": {}, "brief": {}, "rules": {}},
        request={"clip_count": 1},
    )

    # then
    assert registry.adapter.calls == 2
    assert decision["mode"] == "deterministic_fallback"
    assert decision["selected"][0]["candidate_id"] == "a"


def test_a_valid_model_plan_is_cached_against_its_exact_inputs(settings, repos, project):
    # given
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("a", start=0, end=4_000, overall=80)],
    )
    candidates = repos.candidates.list_for_project(project["id"])
    configured = replace(settings, llm_editorial_enabled=True)
    good = {
        "selected": [
            {
                "candidate_id": "a",
                "strategy": "chronological",
                "rationale": "One complete thought.",
                "confidence": 0.6,
                "segments": [{"start_ms": 0, "end_ms": 4_000, "role": "main"}],
                "evidence": ["candidate:a"],
            }
        ]
    }
    registry = FakeRegistry(FakeAdapter(payloads=[good]))

    kwargs = dict(
        project=project,
        source={"duration_ms": 10_000, "has_video": True, "has_audio": True, "content_hash": "abc"},
        candidates=candidates,
        transcript=_transcript(),
        resolved_settings={"captions": {}, "export": {}, "brief": {}, "rules": {}},
        request={"clip_count": 1},
    )

    # when
    first = run_director(repos, configured, registry, **kwargs)
    second = run_director(repos, configured, registry, **kwargs)

    # then
    assert first["mode"] == "model"
    assert registry.adapter.calls == 1
    assert second["metrics"].get("cache") == "hit"


def test_remote_model_direction_requires_explicit_request_consent(settings, repos, project):
    # given a remote provider and no consent flag
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [_candidate("a", start=0, end=4_000, overall=80)],
    )
    candidates = repos.candidates.list_for_project(project["id"])
    configured = replace(settings, llm_editorial_enabled=True, llm_allow_remote=True)
    good = {
        "selected": [
            {
                "candidate_id": "a",
                "strategy": "chronological",
                "segments": [{"start_ms": 0, "end_ms": 4_000, "role": "main"}],
            }
        ]
    }
    adapter = FakeAdapter(payloads=[good, good])
    registry = FakeRegistry(adapter, privacy="remote")
    kwargs = dict(
        project=project,
        source={"duration_ms": 10_000, "has_video": True, "has_audio": True},
        candidates=candidates,
        transcript=_transcript(),
        resolved_settings={"captions": {}, "export": {}, "brief": {}, "rules": {}},
    )

    # when consent is absent
    without = run_director(
        repos, configured, registry, request={"clip_count": 1}, **kwargs
    )
    # then no request was sent
    assert without["mode"] == "deterministic_fallback"
    assert adapter.calls == 0
    assert "consent" in without["notes"].lower()

    # when the request explicitly consents
    with_consent = run_director(
        repos,
        configured,
        registry,
        request={"clip_count": 1, "editorial_consent": True},
        **kwargs,
    )

    # then the model is used
    assert with_consent["mode"] == "model"
    assert adapter.calls == 1


# --- provider registry -------------------------------------------------------


def test_registry_without_configuration_reports_why_it_is_unavailable(settings):
    registry = LlmProviderRegistry(settings)

    info = registry.describe()[0]

    assert info["provider"] == "none"
    assert info["available"] is False
    assert "deterministic" in info["reason"].lower()


def test_remote_provider_is_blocked_unless_remote_inference_is_allowed(settings):
    configured = replace(
        settings,
        llm_enabled=True,
        llm_base_url="https://api.example.com/v1",
        llm_model="remote-model",
        llm_provider="openai_compatible",
        llm_editorial_enabled=True,
    )

    selection = LlmProviderRegistry(configured).select()

    assert selection.available is False
    assert "remote inference is not allowed" in (selection.reason or "")


def test_loopback_openai_compatible_endpoint_is_treated_as_local(settings):
    configured = replace(
        settings,
        llm_enabled=True,
        llm_base_url="http://127.0.0.1:11434/v1",
        llm_model="local-model",
        llm_provider="ollama",
        llm_editorial_enabled=True,
    )

    selection = LlmProviderRegistry(configured).select()

    assert selection.usable is True
    assert selection.info["privacy"] == "local"
    assert isinstance(selection.adapter, OpenAiCompatibleScorer)
    assert selection.adapter.model == "local-model"
    assert is_loopback("http://localhost:8080/v1") is True
    assert is_loopback("https://api.example.com/v1") is False


def test_remote_provider_is_usable_when_an_operator_opts_in(settings):
    configured = replace(
        settings,
        llm_enabled=True,
        llm_base_url="https://api.example.com/v1",
        llm_model="remote-model",
        llm_provider="openai_compatible",
        llm_allow_remote=True,
        llm_editorial_enabled=True,
    )

    selection = LlmProviderRegistry(configured).select()

    assert selection.usable is True
    assert selection.info["privacy"] == "remote"


def test_ollama_defaults_to_the_local_port_with_an_explicit_model(settings):
    configured = replace(
        settings,
        llm_provider="ollama",
        llm_base_url="",
        llm_model="qwen-test",
    )

    selection = LlmProviderRegistry(configured).select()

    assert selection.usable is True
    assert selection.info["base_url"] == "http://127.0.0.1:11434/v1"
