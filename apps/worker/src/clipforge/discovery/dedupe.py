"""Cross-batch duplicate rules for additional discovery.

The rules are intentionally narrower than "similar topic". Two moments may share
a subject and still be distinct; only overlapping source intervals, the same
sentence context, or near-identical wording in the same part of the video are
treated as the same event. A meaningfully different edit of that event is kept.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from clipforge.discovery.coverage import bucket_count, bucket_index
from clipforge.ranking.score import ScoredCandidate
from clipforge.ranking.sentences import TranscriptModel, tokenize

OVERLAP_THRESHOLD = 0.6
TEXT_SIMILARITY_THRESHOLD = 0.75
SENTENCE_OVERLAP_THRESHOLD = 0.8
TEMPORAL_PROXIMITY_MS = 60_000


@dataclass
class DiscoverySelection:
    kept: list[ScoredCandidate] = field(default_factory=list)
    suppressed: list[tuple[ScoredCandidate, str]] = field(default_factory=list)
    duplicate_of_existing: int = 0
    spread_limited: int = 0


def _overlap(first_start: int, first_end: int, second_start: int, second_end: int) -> float:
    start = max(first_start, second_start)
    end = min(first_end, second_end)
    intersection = max(0, end - start)
    if intersection == 0:
        return 0.0
    shortest = min(first_end - first_start, second_end - second_start)
    return intersection / shortest if shortest > 0 else 0.0


def text_similarity(first: str, second: str) -> float:
    a = set(tokenize(first))
    b = set(tokenize(second))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sentence_span(model: TranscriptModel, start_ms: int, end_ms: int) -> tuple[int, int] | None:
    indexes = [
        sentence.index
        for sentence in model.sentences
        if sentence.end_ms > start_ms and sentence.start_ms < end_ms
    ]
    if not indexes:
        return None
    return min(indexes), max(indexes)


def _sentence_overlap(first: tuple[int, int] | None, second: tuple[int, int] | None) -> float:
    if first is None or second is None:
        return 0.0
    start = max(first[0], second[0])
    end = min(first[1], second[1])
    intersection = max(0, end - start + 1)
    shortest = min(first[1] - first[0] + 1, second[1] - second[0] + 1)
    return intersection / shortest if shortest else 0.0


def _temporal_gap(first_start: int, first_end: int, second_start: int, second_end: int) -> int:
    if first_end < second_start:
        return second_start - first_end
    if second_end < first_start:
        return first_start - second_end
    return 0


def meaningfully_different(
    candidate: ScoredCandidate,
    existing: dict,
    model: TranscriptModel,
) -> bool:
    """Keep a second edit only when its boundaries or structure really differ.

    A 2-second trim is not a new moment. A 10-second shift that starts earlier
    and ends later, with materially different text, is.
    """
    start = int(existing.get("start_ms") or 0)
    end = int(existing.get("end_ms") or start)
    overlap = _overlap(candidate.start_ms, candidate.end_ms, start, end)
    if overlap >= 0.9:
        return False
    shortest = min(candidate.duration_ms, max(1, end - start))
    shift = max(8_000, int(shortest * 0.15))
    start_shift = abs(candidate.start_ms - start)
    end_shift = abs(candidate.end_ms - end)
    if start_shift < shift and end_shift < shift:
        return False
    similarity = text_similarity(candidate.excerpt, str(existing.get("excerpt") or ""))
    return similarity < 0.85


def duplicate_reason(
    candidate: ScoredCandidate,
    existing: dict,
    model: TranscriptModel,
) -> str | None:
    start = int(existing.get("start_ms") or 0)
    end = int(existing.get("end_ms") or start)
    overlap = _overlap(candidate.start_ms, candidate.end_ms, start, end)
    if overlap >= OVERLAP_THRESHOLD:
        if meaningfully_different(candidate, existing, model):
            return None
        return f"existing:{existing.get('id', 'unknown')}"

    if _sentence_overlap(
        sentence_span(model, candidate.start_ms, candidate.end_ms),
        sentence_span(model, start, end),
    ) >= SENTENCE_OVERLAP_THRESHOLD:
        return f"existing:{existing.get('id', 'unknown')}"

    similarity = text_similarity(candidate.excerpt, str(existing.get("excerpt") or ""))
    if (
        similarity >= TEXT_SIMILARITY_THRESHOLD
        and _temporal_gap(candidate.start_ms, candidate.end_ms, start, end) <= TEMPORAL_PROXIMITY_MS
    ):
        return f"existing:{existing.get('id', 'unknown')}"
    return None


def _first_duplicate(
    candidate: ScoredCandidate,
    pool: list[ScoredCandidate],
    model: TranscriptModel,
) -> str | None:
    for other in pool:
        if _overlap(candidate.start_ms, candidate.end_ms, other.start_ms, other.end_ms) >= OVERLAP_THRESHOLD:
            if meaningfully_different(
                candidate,
                {
                    "start_ms": other.start_ms,
                    "end_ms": other.end_ms,
                    "excerpt": other.excerpt,
                    "id": other.draft.key,
                },
                model,
            ):
                continue
            return other.draft.key
        if (
            text_similarity(candidate.excerpt, other.excerpt) >= TEXT_SIMILARITY_THRESHOLD
            and _temporal_gap(candidate.start_ms, candidate.end_ms, other.start_ms, other.end_ms) <= TEMPORAL_PROXIMITY_MS
        ):
            return other.draft.key
    return None


def select_additional(
    ranked: list[ScoredCandidate],
    existing: list[dict],
    *,
    limit: int,
    model: TranscriptModel,
    duration_ms: int,
    diversity: str = "balanced",
    revisit_underexplored: bool = True,
    exclude_existing: bool = True,
) -> DiscoverySelection:
    result = DiscoverySelection()

    survivors: list[ScoredCandidate] = []
    for candidate in ranked:
        reason = None
        if exclude_existing:
            for item in existing:
                reason = duplicate_reason(candidate, item, model)
                if reason:
                    break
        if reason:
            candidate.duplicate_of = reason
            result.suppressed.append((candidate, reason))
            result.duplicate_of_existing += 1
            continue
        survivors.append(candidate)

    buckets = bucket_count(duration_ms)
    existing_counts: dict[int, int] = {}
    for item in existing:
        index = bucket_index(int(item.get("start_ms") or 0), duration_ms, buckets)
        existing_counts[index] = existing_counts.get(index, 0) + 1

    def sort_score(candidate: ScoredCandidate) -> float:
        if diversity == "quality" or not revisit_underexplored:
            return candidate.overall
        if diversity == "chronological":
            return -float(candidate.start_ms)
        index = bucket_index(candidate.start_ms, duration_ms, buckets)
        novelty = 1.0 / (1.0 + existing_counts.get(index, 0))
        return candidate.overall * (0.88 + 0.12 * novelty)

    survivors.sort(key=sort_score, reverse=True)

    per_bucket = max(1, math.ceil((len(existing) + limit) / buckets))
    if diversity == "quality" or not revisit_underexplored:
        per_bucket = max(1, limit)
    new_counts: dict[int, int] = {}
    overflow: list[ScoredCandidate] = []

    for candidate in survivors:
        if len(result.kept) >= limit:
            break
        duplicate = _first_duplicate(candidate, result.kept, model)
        if duplicate:
            candidate.duplicate_of = duplicate
            result.suppressed.append((candidate, duplicate))
            continue
        index = bucket_index(candidate.start_ms, duration_ms, buckets)
        if existing_counts.get(index, 0) + new_counts.get(index, 0) >= per_bucket:
            overflow.append(candidate)
            continue
        new_counts[index] = new_counts.get(index, 0) + 1
        result.kept.append(candidate)

    # A narrow source range can put every usable moment in one bucket. The spread
    # rule is a preference, not a reason to return nothing.
    if len(result.kept) < limit:
        for candidate in overflow:
            if len(result.kept) >= limit:
                break
            duplicate = _first_duplicate(candidate, result.kept, model)
            if duplicate:
                candidate.duplicate_of = duplicate
                result.suppressed.append((candidate, duplicate))
                continue
            result.kept.append(candidate)
            result.spread_limited += 1

    result.kept.sort(key=lambda item: item.overall, reverse=True)
    return result
