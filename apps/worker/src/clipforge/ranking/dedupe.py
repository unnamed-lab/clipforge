"""Near-duplicate suppression and timeline spread."""

from __future__ import annotations

from dataclasses import dataclass

from clipforge.ranking.score import ScoredCandidate
from clipforge.ranking.sentences import tokenize

OVERLAP_THRESHOLD = 0.6
TEXT_SIMILARITY_THRESHOLD = 0.7


@dataclass
class SelectionResult:
    kept: list[ScoredCandidate]
    suppressed: list[tuple[ScoredCandidate, str]]


def _overlap_ratio(first: ScoredCandidate, second: ScoredCandidate) -> float:
    start = max(first.start_ms, second.start_ms)
    end = min(first.end_ms, second.end_ms)
    intersection = max(0, end - start)
    if intersection == 0:
        return 0.0
    union = (first.end_ms - first.start_ms) + (second.end_ms - second.start_ms) - intersection
    return intersection / union if union > 0 else 0.0


def _text_similarity(first: ScoredCandidate, second: ScoredCandidate) -> float:
    a = set(tokenize(first.excerpt))
    b = set(tokenize(second.excerpt))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def select_candidates(
    ranked: list[ScoredCandidate],
    *,
    limit: int,
    duration_ms: int,
    allow_near_duplicates: bool = False,
) -> SelectionResult:
    kept: list[ScoredCandidate] = []
    suppressed: list[tuple[ScoredCandidate, str]] = []

    bucket_size = max(1, duration_ms // 10)
    bucket_limit = max(1, (limit + 2) // 3) if limit > 3 else limit
    bucket_counts: dict[int, int] = {}

    for candidate in ranked:
        if len(kept) >= limit:
            break
        duplicate_of: str | None = None
        for existing in kept:
            if _overlap_ratio(candidate, existing) >= OVERLAP_THRESHOLD:
                duplicate_of = existing.draft.key
                break
            if _text_similarity(candidate, existing) >= TEXT_SIMILARITY_THRESHOLD:
                duplicate_of = existing.draft.key
                break
        if duplicate_of is not None:
            candidate.duplicate_of = duplicate_of
            suppressed.append((candidate, duplicate_of))
            continue

        bucket = candidate.start_ms // bucket_size
        if not allow_near_duplicates and bucket_counts.get(bucket, 0) >= bucket_limit:
            candidate.duplicate_of = f"spread:{bucket}"
            suppressed.append((candidate, "timeline_spread"))
            continue

        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        kept.append(candidate)

    # If the spread rule starved the list, top up from the suppressed pool by score.
    if len(kept) < limit:
        for candidate, reason in list(suppressed):
            if len(kept) >= limit:
                break
            if reason != "timeline_spread":
                continue
            candidate.duplicate_of = None
            kept.append(candidate)
            suppressed.remove((candidate, reason))

    kept.sort(key=lambda item: item.overall, reverse=True)
    return SelectionResult(kept=kept, suppressed=suppressed)
