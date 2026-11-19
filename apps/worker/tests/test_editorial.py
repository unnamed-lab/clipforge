"""Editorial intelligence: strategy selection, timelines and duration targets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from clipforge.api.models import RenderRequest
from clipforge.api.routes_renders import _segments_for
from clipforge.briefs.spec import ClippingBrief, EditorialPolicy
from clipforge.durations import preset_range
from clipforge.editorial.narrative import build_retention_plan
from clipforge.editorial.models import RetentionPlan, RetentionSegment
from clipforge.editorial.strategy import build_timeline
from clipforge.pipeline.briefing import _apply_actual_duration
from clipforge.ranking.sentences import build_transcript_model


def _model(lines: list[tuple[int, int, str]]):
    segments = []
    for index, (start, end, text) in enumerate(lines):
        words = text.split()
        step = max(1, (end - start) // max(1, len(words)))
        segments.append(
            {
                "id": index,
                "start_ms": start,
                "end_ms": end,
                "text": text,
                "words": [
                    {
                        "word": word,
                        "start_ms": start + position * step,
                        "end_ms": start + (position + 1) * step,
                    }
                    for position, word in enumerate(words)
                ],
            }
        )
    return build_transcript_model(segments, max(end for _, end, _ in lines))


def _hook(start: int, end: int, text: str) -> dict:
    return {
        "text": text,
        "start_ms": start,
        "end_ms": end,
        "source": "external",
        "placement": "before",
        "direction": "after",
        "approved": True,
        "grounded": True,
        "quality": 82.0,
        "strategy": "outcome",
        "reason": "A later line shares the moment's subject.",
        "needs_context": True,
        "review_required": False,
    }


def _plan(policy: EditorialPolicy | None = None, *, hook: dict | None = None):
    lines = [
        (0, 3_000, "So I started the business in my garage."),
        (3_000, 7_000, "The first year was mostly failed orders."),
        (7_000, 11_000, "Then one customer asked for a hundred units."),
        (11_000, 15_000, "By the end we had shipped ten thousand."),
    ]
    model = _model(lines)
    return build_retention_plan(
        model,
        start_ms=0,
        end_ms=15_000,
        excerpt="I started the business in my garage and eventually shipped ten thousand units.",
        pacing={"profile": "balanced"},
        hook=hook,
        hook_candidates=[hook] if hook else None,
        editorial=policy or EditorialPolicy(),
        duration_preset="extended",
    )


def test_duration_presets_default_to_extended_and_validate_custom_ranges():
    # given no duration fields
    brief = ClippingBrief()

    # then the default is the Extended editorial target
    assert brief.duration_preset == "extended"
    assert (brief.min_duration_ms, brief.max_duration_ms) == (60_000, 90_000)

    # when an old saved range exists it is migrated instead of overwritten
    legacy = ClippingBrief(min_duration_ms=15_000, max_duration_ms=30_000)
    assert legacy.duration_preset == "short"

    # when the user picks Short the preset is authoritative
    short = ClippingBrief(duration_preset="short")
    assert (short.min_duration_ms, short.max_duration_ms) == (15_000, 30_000)

    # then an inverted custom range is rejected
    with pytest.raises(ValidationError):
        ClippingBrief(
            duration_preset="custom", min_duration_ms=60_000, max_duration_ms=40_000
        )


def test_duration_fit_reports_without_changing_the_cut():
    target = preset_range("extended")

    assert target.fit_status(75_000) == "fit"
    assert target.fit_status(59_000) == "near"
    assert target.fit_status(20_000) == "too_short"
    assert "closest coherent cut" in target.describe(95_000)
    assert target.proximity(75_000) == 1.0


def test_cold_open_is_selected_when_a_grounded_later_hook_exists():
    # given a moment with setup, development and payoff plus a later hook
    plan = _plan(hook=_hook(20_000, 23_000, "We shipped ten thousand units."))

    # then the qualified cold open wins and is explained
    assert plan.strategy == "cold_open"
    assert plan.editorial is not None
    assert plan.editorial.reordered is True
    assert plan.editorial.confidence > 0.4
    assert "grounded later line" in plan.editorial.rationale
    assert any(alt.id == "chronological" and alt.available for alt in plan.alternatives)

    # and the timeline opens on the later line, then flashes back
    assert plan.timeline is not None
    assert plan.timeline.valid is True
    assert plan.timeline.entries[0].role == "hook"
    assert plan.timeline.entries[1].transition == "flashback"
    assert plan.timeline.total_output_ms == 15_000 + 3_000


def test_chronological_style_never_reorders_and_keeps_the_baseline():
    policy = EditorialPolicy(style="chronological")

    plan = _plan(policy=policy, hook=_hook(20_000, 23_000, "We shipped ten thousand units."))

    assert plan.strategy == "chronological"
    assert plan.editorial is not None
    assert plan.editorial.reordered is False
    assert plan.editorial.applied is False
    assert plan.timeline is not None
    assert [entry.role for entry in plan.timeline.entries] == ["main", "development", "payoff"]


def test_unavailable_strategy_falls_back_honestly():
    policy = EditorialPolicy(
        style="automatic", preferred_strategy="montage", flashback="avoid"
    )

    plan = _plan(policy=policy, hook=_hook(20_000, 23_000, "We shipped ten thousand units."))

    assert plan.strategy == "chronological"
    assert plan.editorial is not None
    assert plan.editorial.review_required is True
    assert plan.editorial.fallback == "montage"
    assert any("requested but unavailable" in concern for concern in plan.editorial.concerns)


def test_timeline_rejects_duplicated_source_speech():
    # given two beats that cover the same source time
    plan = RetentionPlan(
        segments=[
            RetentionSegment(
                role="main", source_start_ms=0, source_end_ms=5_000, confidence=0.8
            ),
            RetentionSegment(
                role="payoff", source_start_ms=4_000, source_end_ms=9_000, confidence=0.8
            ),
        ],
        confidence=0.8,
    )

    timeline = build_timeline(plan, strategy="chronological")

    assert timeline.valid is False
    assert timeline.duplicated_ms == 1_000
    assert any("duplicated" in warning for warning in timeline.warnings)


def test_timeline_allows_removed_silence_but_rejects_missing_speech():
    # given two kept beats with one second of silence between them
    plan = RetentionPlan(
        segments=[
            RetentionSegment(
                role="main", source_start_ms=0, source_end_ms=3_000, confidence=0.8
            ),
            RetentionSegment(
                role="payoff", source_start_ms=4_000, source_end_ms=7_000, confidence=0.8
            ),
        ],
        confidence=0.8,
    )

    # when the transcript says the gap is silence
    silence = build_timeline(
        plan,
        strategy="chronological",
        speech_intervals=[(0, 3_000), (4_000, 7_000)],
    )

    # then removing the pause is valid and the gap is explained
    assert silence.valid is True
    assert silence.missing_ms == 0
    assert silence.gaps and silence.gaps[0]["speech_ms"] == 0

    # but when speech extends into the gap the plan must not render
    speech = build_timeline(
        plan,
        strategy="chronological",
        speech_intervals=[(0, 3_500), (4_000, 7_000)],
    )

    assert speech.valid is False
    assert speech.missing_ms == 500
    assert any("source speech" in warning for warning in speech.warnings)


def test_actual_duration_reports_the_planned_timeline_not_the_source_window():
    calendar = SimpleNamespace(
        duration={
            "preset": "extended",
            "min_ms": 60_000,
            "max_ms": 90_000,
            "actual_ms": 89_400,
            "status": "fit",
            "reason": "old",
        },
        duration_ms=89_400,
        editorial={"timeline": {"total_output_ms": 92_400}},
        features=SimpleNamespace(warnings=[]),
    )

    _apply_actual_duration(calendar, ClippingBrief(duration_preset="extended"))

    assert calendar.duration["actual_ms"] == 92_400
    assert calendar.duration["status"] == "too_long"
    assert "92.4s" in calendar.duration["reason"]
    assert any("92.4s" in warning for warning in calendar.features.warnings)


def test_manual_trim_beats_the_generated_editorial_timeline():
    # given a candidate whose plan would reorder the source
    plan = _plan(
        policy=EditorialPolicy(preferred_strategy="result_first"), hook=None
    )
    assert plan.editorial is not None
    assert plan.editorial.reordered is True
    candidate = {
        "start_ms": 0,
        "end_ms": 15_000,
        "edited": True,
        "hook_detail": {},
        "editorial": plan.model_dump(),
    }
    payload = RenderRequest(title="test", candidate_id="candidate")

    # when the user has manually trimmed it
    segments, _, editorial = _segments_for(payload, candidate, {"duration_ms": 20_000})

    # then the manual cut wins and no generated reorder is applied
    assert len(segments) == 1
    assert segments[0] == {"start_ms": 0, "end_ms": 15_000, "role": "main"}
    assert editorial["applied"] is False
    assert editorial["fallback"] == "manual_trim"


def test_reviewed_editorial_edit_needs_approval_before_render():
    plan = _plan(policy=EditorialPolicy(preferred_strategy="result_first"), hook=None)
    assert plan.editorial is not None
    assert plan.editorial.review_required is True
    candidate = {
        "start_ms": 0,
        "end_ms": 15_000,
        "edited": False,
        "hook_detail": {},
        "editorial": plan.model_dump(),
    }

    unapproved, _, first = _segments_for(
        RenderRequest(title="test", candidate_id="candidate"), candidate, {"duration_ms": 20_000}
    )
    approved, _, second = _segments_for(
        RenderRequest(title="test", candidate_id="candidate", approve_editorial=True),
        candidate,
        {"duration_ms": 20_000},
    )

    assert first["fallback"] == "review_not_approved"
    assert unapproved[0]["start_ms"] == 0
    assert second["applied"] is True
    assert approved[0]["start_ms"] == 11_000
    assert approved[1]["start_ms"] == 0


def test_editorial_plan_survives_duration_and_pacing_changes_without_reordering_edits():
    plan = _plan(hook=_hook(20_000, 23_000, "We shipped ten thousand units."))

    # the stored plan carries source times, not just labels
    assert plan.timeline is not None
    for entry in plan.timeline.entries:
        assert entry.output_end_ms - entry.output_start_ms == (
            entry.source_end_ms - entry.source_start_ms
        )
    assert plan.segments
    assert [segment.role for segment in plan.segments] == ["main", "development", "payoff"]


def _hook_before(start: int, end: int, text: str) -> dict:
    hook = _hook(start, end, text)
    hook["direction"] = "before"
    hook["needs_context"] = False
    return hook


def test_loop_strategy_replays_the_opening_as_a_deliberate_repeat():
    # given a moment whose opening beat is long enough to replay
    plan = _plan(policy=EditorialPolicy(preferred_strategy="loop"))

    # then the loop is applied instead of being rejected as duplicated speech
    assert plan.editorial is not None
    assert plan.strategy == "loop"
    assert plan.editorial.reordered is True
    assert plan.editorial.review_required is True
    assert plan.timeline is not None
    assert plan.timeline.valid is True
    assert plan.timeline.duplicated_ms == 0
    assert plan.timeline.loop_ms > 0
    loop = plan.timeline.entries[-1]
    assert loop.role == "loop"
    assert (loop.source_start_ms, loop.source_end_ms) == (
        plan.segments[0].source_start_ms,
        plan.segments[0].source_end_ms,
    )
    assert loop.output_start_ms == plan.timeline.total_output_ms - (
        loop.source_end_ms - loop.source_start_ms
    )


def test_a_later_hook_only_opens_a_cold_open_not_a_chronological_cut():
    # given a plan and a grounded later line
    plan = _plan()

    # when it is offered to a chronological cut
    chronological = build_timeline(
        plan,
        strategy="chronological",
        hook_start_ms=20_000,
        hook_end_ms=23_000,
        hook_text="We shipped ten thousand units.",
        hook_direction="after",
    )
    # then it is not played twice in source order
    assert all(entry.role != "hook" for entry in chronological.entries)

    # when the same line opens a deliberate cold open
    cold_open = build_timeline(
        plan,
        strategy="cold_open",
        hook_start_ms=20_000,
        hook_end_ms=23_000,
        hook_text="We shipped ten thousand units.",
        hook_direction="after",
    )
    # then it becomes the first timeline entry
    assert cold_open.entries[0].role == "hook"
    assert cold_open.entries[1].transition == "flashback"


def test_an_earlier_hook_opens_a_chronological_cut_as_context():
    # given a moment that starts after a grounded earlier line
    lines = [
        (0, 2_000, "So I started the business in my garage."),
        (3_000, 7_000, "The first year was mostly failed orders."),
        (7_000, 11_000, "Then one customer asked for a hundred units."),
        (11_000, 15_000, "By the end we had shipped ten thousand."),
    ]
    model = _model(lines)
    plan = build_retention_plan(
        model,
        start_ms=3_000,
        end_ms=15_000,
        excerpt="The first year failed until one customer asked for a hundred units.",
        pacing={"profile": "balanced"},
        editorial=EditorialPolicy(),
        duration_preset="extended",
    )

    # when it is offered to a chronological cut
    timeline = build_timeline(
        plan,
        strategy="chronological",
        hook_start_ms=0,
        hook_end_ms=2_000,
        hook_text="So I started the business in my garage.",
        hook_direction="before",
        speech_intervals=[(sentence.start_ms, sentence.end_ms) for sentence in model.sentences],
    )

    # then it is inserted once, in front of the moment, and nothing repeats
    assert timeline.entries[0].role == "hook"
    assert timeline.entries[1].role != "hook"
    assert timeline.duplicated_ms == 0
    assert timeline.valid is True


def test_chronological_render_only_opens_with_a_hook_that_precedes_the_clip():
    # given the generated plan is unavailable and the renderer falls back to source order
    payload = RenderRequest(title="test", candidate_id="candidate")
    before = {
        "start_ms": 4_000,
        "end_ms": 15_000,
        "edited": False,
        "hook_detail": _hook_before(1_000, 3_500, "So I started the business in my garage."),
        "editorial": {},
    }
    after = {
        "start_ms": 4_000,
        "end_ms": 15_000,
        "edited": False,
        "hook_detail": _hook(20_000, 23_000, "We shipped ten thousand units."),
        "editorial": {},
    }

    # when both are offered
    earlier, _, _ = _segments_for(payload, before, {"duration_ms": 30_000})
    later, _, _ = _segments_for(payload, after, {"duration_ms": 30_000})

    # then only the line that really sits before the clip is played first
    assert earlier[0] == {"start_ms": 1_000, "end_ms": 3_500, "role": "hook"}
    assert earlier[1] == {"start_ms": 4_000, "end_ms": 15_000, "role": "main"}
    assert all(segment["role"] != "hook" for segment in later)


def test_a_hook_that_overlaps_the_clip_is_not_inserted_twice():
    # given a "before" hook that runs into the clip's own opening
    payload = RenderRequest(title="test", candidate_id="candidate")
    candidate = {
        "start_ms": 4_000,
        "end_ms": 15_000,
        "edited": False,
        "hook_detail": _hook_before(3_000, 6_000, "So I started the business in my garage."),
        "editorial": {},
    }

    # when
    segments, _, _ = _segments_for(payload, candidate, {"duration_ms": 30_000})

    # then the overlap is refused rather than repeating source speech
    assert segments == [{"start_ms": 4_000, "end_ms": 15_000, "role": "main"}]
