"""Speaker diarisation interface.

Not implemented in this release. The interface exists so a provider can be added
without touching the pipeline, and the capabilities endpoint reports it as
unavailable so the UI disables speaker controls instead of pretending.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from clipforge.errors import ProviderUnavailable


@dataclass
class SpeakerTurn:
    start_ms: int
    end_ms: int
    speaker: str


class DiarisationProvider(Protocol):
    name: str
    available: bool

    def diarise(self, audio: Path) -> list[SpeakerTurn]: ...


class UnavailableDiarisation:
    name = "none"
    available = False

    def diarise(self, audio: Path) -> list[SpeakerTurn]:
        raise ProviderUnavailable(
            "Speaker separation is not available in this version. Clips use the whole audio track.",
            code="diarisation_unavailable",
        )
