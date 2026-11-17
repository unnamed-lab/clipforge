"""Duration presets and fit reporting.

A preset is a *target range*, not a platform limit. Platform limits live in
``clipforge.platforms`` and are deliberately separate: TikTok allows a much
longer upload than a 60-90 second editorial clip. The preset is what candidate
generation, ranking and the export gate use; the platform only says whether that
clip can be published there.

The rules here are intentionally conservative:

* A candidate is never stretched or truncated to hit a preset.
* A window just outside the range is reported as ``near`` when it is inside the
  configured tolerance, not silently described as a fit.
* No range can be forced; when nothing fits, the caller reports the closest
  coherent alternatives and asks for a review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DurationPreset = Literal["short", "standard", "extended", "custom"]
FitStatus = Literal["fit", "near", "too_short", "too_long", "unknown"]

DURATION_PRESETS: dict[str, tuple[int, int]] = {
    "short": (15_000, 30_000),
    "standard": (30_000, 60_000),
    "extended": (60_000, 90_000),
}

# The underlying clipping limit is looser than any preset. This lets a custom
# brief ask for a 10-second cut without adding a misleading "10 second preset".
MIN_CUSTOM_DURATION_MS = 2_000
MAX_CUSTOM_DURATION_MS = 15 * 60 * 1000
DEFAULT_PRESET: DurationPreset = "extended"
DEFAULT_TOLERANCE_MS = 1_000

PRESET_LABELS: dict[str, str] = {
    "short": "Short · 15–30s",
    "standard": "Standard · 30–60s",
    "extended": "Extended · 60–90s",
    "custom": "Custom",
}


@dataclass(frozen=True)
class DurationRange:
    preset: str
    min_ms: int
    max_ms: int
    target_ms: int | None
    tolerance_ms: int

    @property
    def label(self) -> str:
        if self.preset == "custom":
            return f"Custom · {self.min_ms / 1000:.0f}–{self.max_ms / 1000:.0f}s"
        return PRESET_LABELS.get(self.preset, self.preset)

    def contains(self, duration_ms: int) -> bool:
        return self.min_ms <= duration_ms <= self.max_ms

    def near(self, duration_ms: int) -> bool:
        return (
            self.min_ms - self.tolerance_ms <= duration_ms < self.min_ms
            or self.max_ms < duration_ms <= self.max_ms + self.tolerance_ms
        )

    def fit_status(self, duration_ms: int) -> FitStatus:
        if duration_ms <= 0:
            return "unknown"
        if self.contains(duration_ms):
            return "fit"
        if self.near(duration_ms):
            return "near"
        return "too_short" if duration_ms < self.min_ms else "too_long"

    def proximity(self, duration_ms: int) -> float:
        """0..1 quality of the duration fit, without changing a clip to achieve it."""
        if duration_ms <= 0:
            return 0.0
        if self.contains(duration_ms):
            if self.target_ms is None:
                return 1.0
            span = max(1, self.max_ms - self.min_ms)
            return max(0.75, 1.0 - abs(duration_ms - self.target_ms) / span)
        distance = self.min_ms - duration_ms if duration_ms < self.min_ms else duration_ms - self.max_ms
        return max(0.0, 0.65 - distance / max(1, self.max_ms - self.min_ms))

    def describe(self, duration_ms: int) -> str:
        status = self.fit_status(duration_ms)
        actual = f"{duration_ms / 1000:.1f}s"
        if status == "fit":
            return f"{actual} fits the {self.label} target."
        if status == "near":
            side = "under" if duration_ms < self.min_ms else "over"
            return f"{actual} is just {side} the {self.label} target; the cut was not changed to force a fit."
        side = "shorter" if status == "too_short" else "longer"
        return f"{actual} is {side} than the {self.label} target; the closest coherent cut is shown for review."


def duration_record(duration_range: DurationRange, actual_ms: int) -> dict:
    """One machine-readable duration record shared by analysis, preview and API.

    ``actual_ms`` is the duration the timeline will render, not a padded target.
    """
    actual = max(0, int(actual_ms))
    return {
        "preset": duration_range.preset,
        "label": duration_range.label,
        "min_ms": duration_range.min_ms,
        "max_ms": duration_range.max_ms,
        "target_ms": duration_range.target_ms,
        "tolerance_ms": duration_range.tolerance_ms,
        "actual_ms": actual,
        "status": duration_range.fit_status(actual),
        "proximity": round(duration_range.proximity(actual), 3),
        "reason": duration_range.describe(actual),
    }


def preset_range(
    preset: str | None,
    *,
    min_duration_ms: int | None = None,
    max_duration_ms: int | None = None,
    target_duration_ms: int | None = None,
    tolerance_ms: int = DEFAULT_TOLERANCE_MS,
) -> DurationRange:
    chosen = preset if preset in {*DURATION_PRESETS, "custom"} else DEFAULT_PRESET
    if chosen in DURATION_PRESETS:
        minimum, maximum = DURATION_PRESETS[chosen]
    else:
        minimum = int(min_duration_ms or MIN_CUSTOM_DURATION_MS)
        maximum = int(max_duration_ms or MAX_CUSTOM_DURATION_MS)
        if maximum <= minimum:
            raise ValueError("Maximum duration must be greater than the minimum")
        minimum = max(MIN_CUSTOM_DURATION_MS, minimum)
        maximum = min(MAX_CUSTOM_DURATION_MS, maximum)
    target = target_duration_ms
    if target is not None:
        target = max(minimum, min(int(target), maximum))
    return DurationRange(
        preset=chosen,
        min_ms=minimum,
        max_ms=maximum,
        target_ms=target,
        tolerance_ms=max(0, min(int(tolerance_ms), 15_000)),
    )


def infer_preset(min_duration_ms: int, max_duration_ms: int) -> str:
    for name, (minimum, maximum) in DURATION_PRESETS.items():
        if (minimum, maximum) == (int(min_duration_ms), int(max_duration_ms)):
            return name
    return "custom"


def generation_bands(
    preset: str,
    *,
    min_duration_ms: int | None = None,
    max_duration_ms: int | None = None,
) -> tuple[tuple[int, int, str], ...]:
    """Bands to generate, including alternatives around the requested range.

    Generating only the target band makes a no-fit result look like the source
    had no usable moment. Alternatives are generated too, scored honestly, and
    labelled as alternatives rather than padded into the target.
    """
    if preset == "custom":
        minimum = max(MIN_CUSTOM_DURATION_MS, int(min_duration_ms or MIN_CUSTOM_DURATION_MS))
        maximum = min(MAX_CUSTOM_DURATION_MS, int(max_duration_ms or MAX_CUSTOM_DURATION_MS))
        return ((minimum, maximum, "custom"),)

    chosen = preset if preset in DURATION_PRESETS else DEFAULT_PRESET
    order = ["short", "standard", "extended"]
    target_index = order.index(chosen)
    bands: list[tuple[int, int, str]] = []
    for index, name in enumerate(order):
        minimum, maximum = DURATION_PRESETS[name]
        label = name if index == target_index else f"{name}-alternative"
        bands.append((minimum, maximum, label))
    return tuple(bands)


def fits_platform(platform: dict, duration_ms: int, aspect: str | None = None) -> tuple[bool, str]:
    maximum = int(platform.get("max_duration_ms") or 0)
    minimum = int(platform.get("min_duration_ms") or 0)
    aspects = platform.get("aspects") or []
    if aspect and aspects and aspect not in aspects:
        return False, f"{aspect} is not listed for {platform.get('label', 'this platform')}."
    if maximum and duration_ms > maximum:
        return False, (
            f"{duration_ms / 1000:.1f}s exceeds the configured "
            f"{platform.get('label', 'platform')} maximum of {maximum / 1000:.0f}s."
        )
    if minimum and duration_ms < minimum:
        return False, (
            f"{duration_ms / 1000:.1f}s is below the configured "
            f"{platform.get('label', 'platform')} minimum of {minimum / 1000:.0f}s."
        )
    return True, "Fits the configured platform limit."
