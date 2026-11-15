"""Sentence building, boundary-aware generation, scoring and deduplication."""

from __future__ import annotations

import pytest

from clipforge.ranking.dedupe import OVERLAP_THRESHOLD, select_candidates
from clipforge.ranking.generate import DURATION_BANDS, generate_candidates
from clipforge.ranking.score import BASELINE_VERSION, GOALS, score_candidates, weights_for
from clipforge.ranking.sentences import build_transcript_model


def iou(first, second) -> float:
    start = max(first.start_ms, second.start_ms)
    end = min(first.end_ms, second.end_ms)
    intersection = max(0, end - start)
    if intersection == 0:
        return 0.0
    union = (first.end_ms - first.start_ms) + (second.end_ms - second.start_ms) - intersection
    return intersection / union if union else 0.0



def build_model(segments, sentences):
    return build_transcript_model(segments, duration_ms=segments[-1]["end_ms"] + 1_000, language="en")


# --- transcript model -------------------------------------------------------


def test_build_transcript_model_splits_on_sentence_terminators(sample_segments, sample_sentences):
    # given / when
    model = build_model(sample_segments, sample_sentences)

    # then
    assert len(model.sentences) == len(sample_sentences)
    assert model.sentences[0].text.startswith("The biggest mistake")
    assert all(sentence.ends_with_terminator for sentence in model.sentences)


def test_word_timings_are_monotonic_and_within_the_source(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)

    # when
    words = [word for sentence in model.sentences for word in sentence.words]
    duration = sample_segments[-1]["end_ms"] + 1_000

    # then
    assert words
    assert all(0 <= word.start_ms <= word.end_ms <= duration for word in words)
    assert [word.start_ms for word in words] == sorted(word.start_ms for word in words)


def test_sentences_fall_back_to_even_word_spacing_without_word_timings():
    # given a segment that only has segment-level timings
    segments = [{"id": 0, "start_ms": 0, "end_ms": 4_000, "text": "One two three four five six seven eight."}]

    # when
    model = build_transcript_model(segments, duration_ms=4_000, language="en")

    # then
    words = model.sentences[0].words
    assert len(words) == 8
    assert words[0].start_ms == 0
    assert words[-1].end_ms == 4_000


def test_empty_transcript_produces_no_sentences():
    # given / when
    model = build_transcript_model([], duration_ms=10_000)

    # then
    assert model.sentences == []


# --- candidate generation ---------------------------------------------------


def test_generate_candidates_only_builds_legal_sentence_windows(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)

    # when
    drafts = generate_candidates(model)

    # then
    assert drafts
    for draft in drafts:
        first = model.sentences[draft.sentence_start]
        last = model.sentences[draft.sentence_end]
        # start pulls back at most the configured lead-in, end extends at most the tail
        assert 0 <= first.start_ms - draft.start_ms <= 400
        assert 0 <= draft.end_ms - last.end_ms <= 600
        assert draft.duration_ms > 0
        assert draft.start_ms >= 0


def test_generate_candidates_respects_requested_duration_bands(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)
    short_band = (DURATION_BANDS[0],)

    # when
    drafts = generate_candidates(model, bands=short_band)

    # then
    assert drafts
    for draft in drafts:
        assert 15_000 - 1_000 <= draft.duration_ms <= 30_000 + 1_000
        assert draft.band == "short"


def test_generate_candidates_returns_nothing_for_an_empty_model():
    # given
    model = build_transcript_model([], duration_ms=60_000)

    # when / then
    assert generate_candidates(model) == []


# --- scoring ----------------------------------------------------------------


@pytest.mark.parametrize("goal", [goal for goal in GOALS if goal != "custom"])
def test_goal_weights_sum_to_one(goal):
    # given / when
    weights = weights_for(goal)

    # then
    assert abs(sum(weights.values()) - 1.0) < 0.01
    assert all(value >= 0 for value in weights.values())


def test_weight_overrides_are_renormalised():
    # given / when
    weights = weights_for("social_clips", {"hook": 10.0, "emotion": 0.0})

    # then
    assert abs(sum(weights.values()) - 1.0) < 0.01
    assert weights["hook"] > weights["emotion"]


def test_unknown_weight_keys_are_ignored():
    # given / when
    weights = weights_for("best_moments", {"not_a_component": 99.0})

    # then
    assert "not_a_component" not in weights


def test_scored_candidates_expose_ten_bounded_components(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)

    # when
    scored = score_candidates(generate_candidates(model), model, goal="best_moments")

    # then
    assert scored
    for candidate in scored:
        payload = candidate.scores_payload()
        assert 0 <= candidate.overall <= 100
        assert len(payload["components"]) == 10
        for component in payload["components"].values():
            assert 0 <= component["score"] <= 100
            assert component["reason"]
        assert payload["method"] == "baseline"
        assert payload["method_version"] == BASELINE_VERSION
        assert payload["estimated"] is True


def test_every_candidate_has_a_title_and_a_reason(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)

    # when
    scored = score_candidates(generate_candidates(model), model)

    # then
    for candidate in scored:
        assert candidate.title and candidate.title != "Untitled moment"
        assert len(candidate.reason) > 20


def test_scored_candidates_are_ordered_by_overall_score(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)

    # when
    scored = score_candidates(generate_candidates(model), model)

    # then
    assert [candidate.overall for candidate in scored] == sorted(
        (candidate.overall for candidate in scored), reverse=True
    )


def test_goal_changes_the_ranking_weights_used(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)
    drafts = generate_candidates(model)

    # when
    social = score_candidates(drafts, model, goal="social_clips")[0]
    educational = score_candidates(drafts, model, goal="educational")[0]

    # then
    assert social.weight_map["hook"] > social.weight_map["density"]
    assert educational.weight_map["density"] > educational.weight_map["hook"]


# --- deduplication ----------------------------------------------------------


def test_selection_never_keeps_two_heavily_overlapping_candidates(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)
    scored = score_candidates(generate_candidates(model), model)

    # when
    selection = select_candidates(scored, limit=5, duration_ms=model.duration_ms)

    # then
    assert 0 < len(selection.kept) <= 5
    for index, candidate in enumerate(selection.kept):
        for other in selection.kept[index + 1 :]:
            assert _overlap_ratio(candidate, other) < OVERLAP_THRESHOLD


def test_selection_records_why_candidates_were_suppressed(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)
    scored = score_candidates(generate_candidates(model), model)

    # when
    selection = select_candidates(scored, limit=3, duration_ms=model.duration_ms)

    # then
    assert selection.suppressed
    assert all(reason for _, reason in selection.suppressed)
    assert all(candidate.duplicate_of for candidate, _ in selection.suppressed if reason != "timeline_spread")


def test_selection_caps_the_result_at_the_requested_count(sample_segments, sample_sentences):
    # given
    model = build_model(sample_segments, sample_sentences)
    scored = score_candidates(generate_candidates(model), model)

    # when
    selection = select_candidates(scored, limit=2, duration_ms=model.duration_ms)

    # then
    assert len(selection.kept) <= 2


def test_selection_is_empty_for_an_empty_ranking():
    # given / when
    selection = select_candidates([], limit=5, duration_ms=60_000)

    # then
    assert selection.kept == []
    assert selection.suppressed == []
