"""Interval coverage for a source, so "generate more" can revisit blind spots.

Coverage is measured on the source timeline, never on output timelines. A bucket
is explored when candidates actually cover part of it, not merely because a
candidate starts inside it: two 20-second clips at the same minute leave the
rest of a five-minute section untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

BUCKET_MS = 120_000
MIN_BUCKETS = 4
MAX_BUCKETS = 20


def bucket_count(duration_ms: int) -> int:
    return max(MIN_BUCKETS, min(MAX_BUCKETS, math.ceil(max(1, duration_ms) / BUCKET_MS)))


def bucket_index(start_ms: int, duration_ms: int, buckets: int | None = None) -> int:
    total = buckets or bucket_count(duration_ms)
    width = max(1, math.ceil(max(1, duration_ms) / total))
    return max(0, min(total - 1, int(start_ms) // width))


def _union_ms(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += max(0, current_end - current_start)
            current_start, current_end = start, end
    total += max(0, current_end - current_start)
    return total


@dataclass
class Bucket:
    index: int
    start_ms: int
    end_ms: int
    candidates: int = 0
    new_candidates: int = 0
    covered_ms: int = 0
    status: str = "unexplored"

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    @property
    def ratio(self) -> float:
        return round(self.covered_ms / self.duration_ms, 3) if self.duration_ms else 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "candidates": self.candidates,
            "new_candidates": self.new_candidates,
            "covered_ms": self.covered_ms,
            "ratio": self.ratio,
            "status": self.status,
        }


def _clamp(interval: tuple[int, int], start: int, end: int) -> tuple[int, int] | None:
    clipped = (max(start, interval[0]), min(end, interval[1]))
    return clipped if clipped[1] > clipped[0] else None


def build_coverage(
    duration_ms: int,
    existing: list[dict],
    added: list[dict] | None = None,
) -> list[Bucket]:
    added = added or []
    total = max(1, duration_ms)
    count = bucket_count(total)
    width = max(1, math.ceil(total / count))
    buckets = [
        Bucket(index=index, start_ms=index * width, end_ms=min(total, (index + 1) * width))
        for index in range(count)
    ]
    for bucket in buckets:
        if bucket.start_ms >= total:
            bucket.end_ms = bucket.start_ms

    for source, is_new in ((existing, False), (added, True)):
        for candidate in source:
            start = int(candidate.get("start_ms") or 0)
            end = int(candidate.get("end_ms") or start)
            if end <= start:
                continue
            index = bucket_index(start, total, count)
            bucket = buckets[index]
            bucket.candidates += 1
            if is_new:
                bucket.new_candidates += 1
            clipped = _clamp((start, end), bucket.start_ms, bucket.end_ms)
            if clipped:
                bucket.covered_ms += clipped[1] - clipped[0]

    # Merge overlaps per bucket. The sum above can double-count overlapping clips;
    # recomputing from the real intervals keeps the ratio honest.
    for bucket in buckets:
        intervals = []
        for source in (existing, added):
            for candidate in source:
                clipped = _clamp(
                    (int(candidate.get("start_ms") or 0), int(candidate.get("end_ms") or 0)),
                    bucket.start_ms,
                    bucket.end_ms,
                )
                if clipped:
                    intervals.append(clipped)
        bucket.covered_ms = _union_ms(intervals)
        if bucket.candidates == 0:
            bucket.status = "unexplored"
        elif bucket.ratio < 0.2 or bucket.candidates == 1:
            bucket.status = "thin"
        else:
            bucket.status = "covered"
    return buckets


def coverage_summary(buckets: list[Bucket]) -> dict:
    total = sum(bucket.duration_ms for bucket in buckets)
    covered = sum(bucket.covered_ms for bucket in buckets)
    return {
        "duration_ms": total,
        "covered_ms": covered,
        "ratio": round(covered / total, 3) if total else 0.0,
        "buckets": [bucket.to_dict() for bucket in buckets],
        "unexplored": sum(1 for bucket in buckets if bucket.status == "unexplored"),
        "thin": sum(1 for bucket in buckets if bucket.status == "thin"),
        "covered": sum(1 for bucket in buckets if bucket.status == "covered"),
    }


def underexplored(buckets: list[Bucket]) -> list[Bucket]:
    return [bucket for bucket in buckets if bucket.status != "covered"]
