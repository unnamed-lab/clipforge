from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ScorableClip:
    id: str
    start_ms: int
    end_ms: int
    text: str
    baseline_overall: float


@dataclass
class LlmClipScore:
    id: str
    title: str
    hook: str
    reason: str
    category: str
    overall: float
    components: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LlmScoringResult:
    provider: str
    model: str
    clips: list[LlmClipScore]


class LlmScoringProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str | None]: ...

    def score(self, clips: list[ScorableClip], *, goal: str, language: str | None) -> LlmScoringResult: ...
