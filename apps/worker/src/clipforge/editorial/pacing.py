"""Safe, deterministic pacing.

Pacing is not "cut every pause". It removes only the middle of a long silence
between two transcript sentences, leaves a configurable breath on both sides,
and refuses to touch the first or last boundary of the chosen moment. If a cut
would be large enough to change meaning or leave too little speech, it is
reported for review instead of being applied silently.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from clipforge.briefs.spec import PacingPolicy
from clipforge.editorial.models import TimingCut, TimingPlan, TimingSegment
from clipforge.ranking.lexicon import FILLER_STARTS
from clipforge.ranking.sentences import Sentence, TranscriptModel, tokenize

MAX_REMOVED_FRACTION = 0.35
MIN_OUTPUT_MS = 2_000


@dataclass(frozen=True)
class PacingSettings:
    profile: str
    remove_dead_air: bool
    remove_fillers: bool
    max_pause_ms: int
    keep_pause_ms: int
    max_trim_ms: int
    min_segment_ms: int
    pattern_interrupts: bool


PACING_PROFILES: dict[str, PacingSettings] = {
    "calm": PacingSettings("calm", False, False, 1_400, 260, 0, 1_200, False),
    "conversational": PacingSettings("conversational", True, False, 1_100, 220, 1_500, 1_000, True),
    "balanced": PacingSettings("balanced", True, False, 900, 160, 1_400, 900, True),
    "energetic": PacingSettings("energetic", True, True, 650, 120, 1_200, 750, True),
    "fast": PacingSettings("fast", True, True, 450, 80, 1_000, 650, True),
}


def resolve_pacing(policy: PacingPolicy | dict | None) -> PacingSettings:
    if policy is None:
        policy = PacingPolicy()
    if isinstance(policy, dict):
        policy = PacingPolicy.model_validate(policy)
    base = PACING_PROFILES.get(policy.profile, PACING_PROFILES["balanced"])
    overrides = {
        "remove_dead_air": policy.remove_dead_air,
        "remove_fillers": policy.remove_fillers,
        "max_pause_ms": policy.max_pause_ms,
        "keep_pause_ms": policy.keep_pause_ms,
        "max_trim_ms": policy.max_trim_ms,
        "min_segment_ms": policy.min_segment_ms,
        "pattern_interrupts": policy.pattern_interrupts,
    }
    cleaned = {key: value for key, value in overrides.items() if value is not None}
    resolved = replace(base, **cleaned)
    if resolved.keep_pause_ms >= resolved.max_pause_ms:
        resolved = replace(resolved, keep_pause_ms=max(0, resolved.max_pause_ms // 5))
    return resolved


def _is_filler(sentence: Sentence) -> bool:
    tokens = tokenize(sentence.text)
    if not tokens or len(tokens) > 4:
        return False
    filler_tokens = set()
    for marker in FILLER_STARTS:
        filler_tokens.update(tokenize(marker))
    filler_tokens.update({"um", "uh", "erm", "ah", "like", "you", "know", "i", "mean", "so", "okay", "ok"})
    meaningful = [token for token in tokens if token not in filler_tokens]
    return not meaningful


def _sentences_in(model: TranscriptModel, start_ms: int, end_ms: int) -> list[Sentence]:
    return [
        sentence
        for sentence in model.sentences
        if sentence.end_ms > start_ms and sentence.start_ms < end_ms
    ]


def _pieces(
    sentences: list[Sentence],
    start_ms: int,
    end_ms: int,
    *,
    remove_fillers: bool,
) -> tuple[list[dict], list[TimingCut]]:
    pieces: list[dict] = []
    cuts: list[TimingCut] = []
    for position, sentence in enumerate(sentences):
        piece_start = max(start_ms, sentence.start_ms)
        piece_end = min(end_ms, sentence.end_ms)
        if piece_end <= piece_start:
            continue
        is_edge = position == 0 or position == len(sentences) - 1
        if remove_fillers and not is_edge and _is_filler(sentence):
            cuts.append(
                TimingCut(
                    kind="filler",
                    source_start_ms=piece_start,
                    source_end_ms=piece_end,
                    duration_ms=piece_end - piece_start,
                    reason=f"Removed a standalone filler line: {sentence.text[:80]}",
                )
            )
            continue
        pieces.append(
            {
                "start_ms": piece_start,
                "end_ms": piece_end,
                "sentence_start": sentence.index,
                "sentence_end": sentence.index,
            }
        )
    return pieces, cuts


def _trim_gaps(pieces: list[dict], settings: PacingSettings) -> tuple[list[dict], list[TimingCut]]:
    if not settings.remove_dead_air or settings.max_trim_ms <= 0 or len(pieces) < 2:
        return pieces, []
    trimmed = [dict(piece) for piece in pieces]
    cuts: list[TimingCut] = []
    keep_left = settings.keep_pause_ms // 2
    keep_right = settings.keep_pause_ms - keep_left
    for index in range(len(trimmed) - 1):
        left = trimmed[index]
        right = trimmed[index + 1]
        gap = right["start_ms"] - left["end_ms"]
        if gap <= settings.max_pause_ms:
            continue
        cut_start = left["end_ms"] + keep_left
        cut_end = right["start_ms"] - keep_right
        if cut_end <= cut_start:
            continue
        if cut_end - cut_start > settings.max_trim_ms:
            cut_end = cut_start + settings.max_trim_ms
        if cut_end - cut_start < 250:
            continue
        # Never cross a sentence boundary: the entire cut lives inside the
        # silence between two already-complete sentences.
        if cut_start <= left["end_ms"] or cut_end >= right["start_ms"]:
            continue
        left["end_ms"] = cut_start
        right["start_ms"] = cut_end
        cuts.append(
            TimingCut(
                kind="dead_air",
                source_start_ms=cut_start,
                source_end_ms=cut_end,
                duration_ms=cut_end - cut_start,
                reason=(
                    f"Trimmed {((cut_end - cut_start) / 1000):.2f}s from a "
                    f"{gap / 1000:.2f}s pause, keeping {settings.keep_pause_ms}ms of breath."
                ),
            )
        )
    return trimmed, cuts


def _merge_touching(pieces: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for piece in pieces:
        if merged and piece["start_ms"] <= merged[-1]["end_ms"] + 25:
            merged[-1]["end_ms"] = max(merged[-1]["end_ms"], piece["end_ms"])
            merged[-1]["sentence_end"] = max(merged[-1]["sentence_end"], piece["sentence_end"])
            continue
        merged.append(dict(piece))
    return merged


def plan_timing(
    model: TranscriptModel,
    *,
    start_ms: int,
    end_ms: int,
    policy: PacingPolicy | dict | None = None,
) -> TimingPlan:
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 1, int(end_ms))
    original = end_ms - start_ms
    settings = resolve_pacing(policy)
    sentences = _sentences_in(model, start_ms, end_ms)
    pieces, filler_cuts = _pieces(
        sentences,
        start_ms,
        end_ms,
        remove_fillers=settings.remove_fillers,
    )
    if not pieces:
        pieces = [{"start_ms": start_ms, "end_ms": end_ms, "sentence_start": None, "sentence_end": None}]
        filler_cuts = []
    pieces, gap_cuts = _trim_gaps(pieces, settings)
    pieces = _merge_touching(pieces)

    # Never return a plan that is too short to export. The original span is the
    # safe fallback; the failed attempt is still explained in the warnings.
    output_ms = sum(piece["end_ms"] - piece["start_ms"] for piece in pieces)
    warnings: list[str] = []
    review_required = False
    cuts = filler_cuts + gap_cuts
    if output_ms < MIN_OUTPUT_MS:
        warnings.append("Pacing would make the clip shorter than two seconds; the original timing was kept.")
        pieces = [{"start_ms": start_ms, "end_ms": end_ms, "sentence_start": None, "sentence_end": None}]
        cuts = []
        output_ms = original
        review_required = True
    removed = max(0, original - output_ms)
    if original and removed / original > MAX_REMOVED_FRACTION:
        review_required = True
        warnings.append(
            f"Pacing would remove {removed / 1000:.1f}s ({removed / original:.0%}) of the moment. "
            "Review the cut list before publishing."
        )
    if settings.remove_fillers and filler_cuts:
        warnings.append("Standalone filler lines were removed by the selected pacing profile.")

    segments = [
        TimingSegment(
            source_start_ms=piece["start_ms"],
            source_end_ms=piece["end_ms"],
            sentence_start=piece.get("sentence_start"),
            sentence_end=piece.get("sentence_end"),
            reason="kept" if not cuts else "kept after pacing",
        )
        for piece in pieces
    ]
    return TimingPlan(
        profile=settings.profile,
        segments=segments,
        cuts=cuts,
        original_duration_ms=original,
        output_duration_ms=output_ms,
        removed_ms=removed,
        warnings=warnings,
        review_required=review_required,
    )
