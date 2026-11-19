"""Pure policy for choosing what to download and how to prepare a source.

Nothing here touches the network, the filesystem or the database. Given the
metadata an extractor already returned, these functions decide which streams to
fetch, whether a proxy is worth building, and what to tell the user about the
choice. Keeping it pure means every branch (unavailable resolution, audio-only,
size estimates) is testable without a download.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# The user-facing ladder. Values are the cap in pixels, not a promise: a request
# is only ever satisfied by a format the source actually publishes.
QUALITY_LADDER: tuple[tuple[str, str, int | None], ...] = (
    ("lowest", "Lowest available", None),
    ("144p", "144p", 144),
    ("240p", "240p", 240),
    ("360p", "360p", 360),
    ("480p", "480p", 480),
    ("720p", "720p", 720),
    ("1080p", "1080p", 1080),
    ("1440p", "1440p / 2K", 1440),
    ("2160p", "2160p / 4K", 2160),
    ("best", "Best available", None),
    ("audio", "Audio only", 0),
)
QUALITY_IDS = tuple(item[0] for item in QUALITY_LADDER)
DEFAULT_QUALITY = "720p"
MIN_VIDEO_HEIGHT = 100
AUDIO_ABR_TARGET_KBPS = 192

PROCESSING_MODES: dict[str, dict[str, Any]] = {
    "fast": {
        "label": "Fast preview",
        "quality": "480p",
        "proxy_height": 360,
        "transcription_quality": "fast",
        "scene_fps": 0.5,
        "warnings": [
            "Fast preview downloads a small rendition. Small on-screen text may be unreadable.",
        ],
    },
    "balanced": {
        "label": "Balanced",
        "quality": "720p",
        "proxy_height": 480,
        "transcription_quality": "balanced",
        "scene_fps": 1.0,
        "warnings": [],
    },
    "high": {
        "label": "High quality",
        "quality": "1080p",
        "proxy_height": 720,
        "transcription_quality": "best",
        "scene_fps": 1.0,
        "warnings": [
            "High quality downloads a larger rendition. This is the right choice for slides, charts and documents.",
        ],
    },
}
CUSTOM_MODE = "custom"
PROCESSING_IDS = (*PROCESSING_MODES, CUSTOM_MODE)
MIN_PROXY_HEIGHT = 144
MAX_PROXY_HEIGHT = 1440


class PolicyError(ValueError):
    """A request that policy cannot satisfy, with a user-safe message."""

    def __init__(self, message: str, *, code: str = "invalid_download_request") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class FormatOption:
    format_id: str
    height: int | None
    width: int | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    ext: str
    tbr: float | None
    abr: float | None
    filesize: int | None
    protocol: str | None
    audio_channels: int | None
    language: str | None = None

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec and self.vcodec != "none" and self.height)

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec and self.acodec != "none")


@dataclass
class FormatChoice:
    quality: str
    requested_quality: str
    video: FormatOption | None
    audio: FormatOption | None
    duration_ms: int
    estimated_bytes: int | None
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    container: str
    has_video: bool
    explanation: str
    warnings: list[str] = field(default_factory=list)

    @property
    def selector(self) -> str:
        """yt-dlp format expression that downloads only the chosen streams."""
        video_id = self.video.format_id if self.video else None
        audio_id = self.audio.format_id if self.audio else None
        if video_id and audio_id:
            return f"{video_id}+{audio_id}"
        if video_id:
            return video_id
        if audio_id:
            return audio_id
        return "bestaudio/best"


@dataclass(frozen=True)
class IngestPlan:
    mode: str
    quality: str
    proxy_height: int | None
    transcription_quality: str
    scene_fps: float
    download_proxy: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "quality": self.quality,
            "proxy_height": self.proxy_height,
            "transcription_quality": self.transcription_quality,
            "scene_fps": self.scene_fps,
            "download_proxy": self.download_proxy,
            "warnings": list(self.warnings),
        }


def normalise_quality(value: str | None, *, default: str = DEFAULT_QUALITY) -> str:
    if not value:
        return default
    lowered = value.strip().lower()
    aliases = {
        "2k": "1440p",
        "4k": "2160p",
        "2160": "2160p",
        "1440": "1440p",
        "1080": "1080p",
        "720": "720p",
        "480": "480p",
        "360": "360p",
        "240": "240p",
        "144": "144p",
        "audio_only": "audio",
        "audio-only": "audio",
        "best available": "best",
    }
    lowered = aliases.get(lowered, lowered)
    if lowered not in QUALITY_IDS:
        raise PolicyError(
            f"'{value}' is not a download quality. Choose one of: {', '.join(QUALITY_IDS)}.",
            code="invalid_quality",
        )
    return lowered


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _size(format_data: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = _number(format_data.get(key))
        if value:
            return int(value)
    return None


def collect_formats(info: dict) -> list[FormatOption]:
    options: list[FormatOption] = []
    for raw in info.get("formats") or []:
        if not isinstance(raw, dict):
            continue
        format_id = str(raw.get("format_id") or "").strip()
        if not format_id:
            continue
        language = raw.get("language")
        options.append(
            FormatOption(
                format_id=format_id,
                height=int(raw["height"]) if _number(raw.get("height")) else None,
                width=int(raw["width"]) if _number(raw.get("width")) else None,
                fps=_number(raw.get("fps")),
                vcodec=(raw.get("vcodec") or None),
                acodec=(raw.get("acodec") or None),
                ext=str(raw.get("ext") or "mp4"),
                tbr=_number(raw.get("tbr")),
                abr=_number(raw.get("abr")),
                filesize=_size(raw),
                protocol=raw.get("protocol"),
                audio_channels=int(raw["audio_channels"]) if _number(raw.get("audio_channels")) else None,
                language=str(language) if language else None,
            )
        )
    if not options:
        # Some extractors return a single progressive stream at the top level.
        height = _number(info.get("height"))
        if height:
            options.append(
                FormatOption(
                    format_id="best",
                    height=int(height),
                    width=int(info["width"]) if _number(info.get("width")) else None,
                    fps=_number(info.get("fps")),
                    vcodec=(info.get("vcodec") or "unknown"),
                    acodec=(info.get("acodec") or None),
                    ext=str(info.get("ext") or "mp4"),
                    tbr=_number(info.get("tbr")),
                    abr=_number(info.get("abr")),
                    filesize=_size(info),
                    protocol=info.get("protocol"),
                    audio_channels=None,
                )
            )
    return options


def _video_options(options: Iterable[FormatOption], *, max_height: int | None) -> list[FormatOption]:
    videos = [
        option for option in options
        if option.has_video and (max_height is None or (option.height or 0) <= max_height)
    ]
    return sorted(
        videos,
        key=lambda option: (
            option.height or 0,
            option.filesize or int((option.tbr or 1_000) * 1_000),
            option.format_id,
        ),
    )


def _audio_options(options: Iterable[FormatOption]) -> list[FormatOption]:
    audios = [option for option in options if option.has_audio and not option.has_video]
    return sorted(
        audios,
        key=lambda option: (
            option.abr or 0,
            option.filesize or 0,
            option.format_id,
        ),
        reverse=True,
    )


def available_qualities(options: Iterable[FormatOption]) -> list[int]:
    heights = sorted({option.height for option in options if option.has_video and option.height})
    return [height for height in heights if height >= MIN_VIDEO_HEIGHT]


def _target_height(quality: str, heights: list[int], *, max_height: int | None) -> tuple[int | None, bool]:
    """Return the height to use and whether the exact request was met."""
    capped = [height for height in heights if max_height is None or height <= max_height]
    pool = capped or heights
    if not pool:
        return None, False
    if quality == "lowest":
        return min(pool), True
    if quality == "best":
        return max(pool), True
    requested = dict((item[0], item[2]) for item in QUALITY_LADDER).get(quality) or int(quality.rstrip("p"))
    under = [height for height in pool if height <= requested]
    if requested in pool:
        return requested, True
    if under:
        # Do not silently exceed the requested size; take the best that fits.
        return max(under), False
    # Everything published is larger than requested. The smallest is the
    # closest honest option, and the caller records the compromise.
    return min(pool), False


def _estimate_from_bitrate(option: FormatOption, duration_s: float) -> int | None:
    if option.filesize:
        return option.filesize
    if option.tbr and duration_s > 0:
        return int(option.tbr * 1_000 / 8 * duration_s)
    return None


def estimate_height_bps(height: int) -> int:
    """A labelled approximation, used only when the extractor gives no size."""
    table = {144: 150_000, 240: 300_000, 360: 600_000, 480: 1_000_000, 720: 2_500_000,
             1080: 5_000_000, 1440: 10_000_000, 2160: 22_000_000}
    closest = min(table, key=lambda key: abs(key - height))
    return table[closest]


def choose_format(
    info: dict,
    quality: str | None = None,
    *,
    max_height: int | None = None,
    default_quality: str = DEFAULT_QUALITY,
) -> FormatChoice:
    requested = normalise_quality(quality, default=default_quality)
    options = collect_formats(info)
    duration_s = float(_number(info.get("duration")) or 0)
    duration_ms = int(round(duration_s * 1000))
    heights = available_qualities(options)
    warnings: list[str] = []

    if requested == "audio":
        audios = _audio_options(options)
        chosen = audios[0] if audios else None
        if chosen is None:
            raise PolicyError("This source has no audio stream to download.", code="audio_unavailable")
        estimated = _estimate_from_bitrate(chosen, duration_s)
        if estimated is None and chosen.abr and duration_s:
            estimated = int(chosen.abr * 1_000 / 8 * duration_s)
        return FormatChoice(
            quality="audio",
            requested_quality=requested,
            video=None,
            audio=chosen,
            duration_ms=duration_ms,
            estimated_bytes=estimated,
            width=None,
            height=None,
            fps=None,
            video_codec=None,
            audio_codec=_codec_name(chosen.acodec),
            container=chosen.ext,
            has_video=False,
            explanation=(
                "Audio only: no video stream is downloaded. Transcription works, but a video "
                "download is required before exporting a clip."
            ),
            warnings=["Audio-only ingestion cannot produce a video export."],
        )

    if not heights:
        raise PolicyError("This source has no downloadable video stream.", code="video_unavailable")

    target, exact = _target_height(requested, heights, max_height=max_height)
    pool = [option for option in _video_options(options, max_height=max_height) if option.height == target]
    # Prefer the leanest video-only stream so the audio can come from the best
    # audio rendition; a progressive stream is only used when it is all that is
    # published at this height.
    video = min(
        pool,
        key=lambda option: (
            0 if not option.has_audio else 1,
            option.filesize or int((option.tbr or 1_000) * 1_000),
            option.format_id,
        ),
    )
    audio: FormatOption | None = None
    if not video.has_audio:
        audios = _audio_options(options)
        if audios:
            audio = min(
                audios,
                key=lambda option: (
                    # Prefer the highest bitrate that stays under the target.
                    0 if (option.abr or 0) <= AUDIO_ABR_TARGET_KBPS else 1,
                    -int(option.abr or 0),
                    option.filesize or int((option.tbr or 1_000) * 1_000),
                ),
            )

    estimated_parts = [
        value for value in (
            _estimate_from_bitrate(video, duration_s),
            _estimate_from_bitrate(audio, duration_s) if audio else None,
        ) if value
    ]
    estimated = sum(estimated_parts) if estimated_parts else None
    if estimated is None and duration_s:
        estimated = int(estimate_height_bps(target or 720) / 8 * duration_s)

    height_label = f"{target}p" if target else "unknown height"
    if exact:
        explanation = (
            f"{height_label} is available; downloading format {video.format_id}"
            f" ({_codec_name(video.vcodec)}, {video.width or '?'}×{video.height or '?'}"
            f"{f', {video.fps:.0f} fps' if video.fps else ''})."
        )
    else:
        closest = f"{target}p" if target else "an unknown resolution"
        explanation = (
            f"{requested} is not offered by this source. Using the closest available rendition, "
            f"{closest} (format {video.format_id})."
        )
        warnings.append(f"{requested} is unavailable; {closest} was selected instead.")
    if audio is not None:
        explanation += f" Audio is taken from format {audio.format_id}."
    if not (video.has_audio or audio is not None):
        warnings.append(
            "This rendition has no audio stream; transcription and audio-led editing will not be available."
        )
    if max_height is not None and target and target > max_height:
        warnings.append(f"The download was capped at {max_height}p by the processing mode.")

    return FormatChoice(
        quality=f"{target}p" if target else requested,
        requested_quality=requested,
        video=video,
        audio=audio,
        duration_ms=duration_ms,
        estimated_bytes=estimated,
        width=video.width,
        height=video.height,
        fps=video.fps,
        video_codec=_codec_name(video.vcodec),
        audio_codec=_codec_name(audio.acodec if audio else video.acodec),
        container="mp4" if video.ext in {"mp4", "m4v"} else video.ext,
        has_video=True,
        explanation=explanation,
        warnings=warnings,
    )


def _codec_name(value: str | None) -> str | None:
    if not value or value == "none":
        return None
    # yt-dlp reports codec strings such as "avc1.640028"; the family is enough.
    return value.split(".", 1)[0]


def download_options(info: dict) -> list[dict]:
    """The ladder, annotated with what this source can actually satisfy.

    ``available`` means the exact rung exists. When it does not, the UI still
    shows the closest rendition that would be used, so a request is never
    silently upgraded to a resolution the source does not publish.
    """
    options = collect_formats(info)
    heights = available_qualities(options)
    duration_s = float(_number(info.get("duration")) or 0)
    result: list[dict] = []
    for quality_id, label, cap in QUALITY_LADDER:
        closest: int | None = None
        fallback = False
        if quality_id == "audio":
            audios = _audio_options(options)
            available = bool(audios)
            estimated = _estimate_from_bitrate(audios[0], duration_s) if audios else None
        elif not heights:
            available = False
            estimated = None
        elif quality_id in {"lowest", "best"}:
            available = True
            closest = min(heights) if quality_id == "lowest" else max(heights)
            estimated = _estimate_for_height(options, closest, duration_s)
        else:
            target, exact = _target_height(quality_id, heights, max_height=None)
            closest = target
            fallback = not exact
            # A rung is only "available" when the exact height exists; a fallback
            # rung is still selectable and explains the compromise before download.
            available = exact
            estimated = _estimate_for_height(options, target, duration_s) if target else None
        result.append(
            {
                "id": quality_id,
                "label": label,
                "max_height": cap,
                "available": available,
                "closest_height": closest,
                "fallback": fallback,
                "estimated_bytes": estimated,
                "approximate": True,
            }
        )
    return result


def _estimate_for_height(options: list[FormatOption], height: int | None, duration_s: float) -> int | None:
    if height is None:
        return None
    pool = [option for option in options if option.has_video and option.height == height]
    if not pool:
        return None
    video = min(pool, key=lambda option: option.filesize or int((option.tbr or 1_000) * 1_000))
    video_bytes = _estimate_from_bitrate(video, duration_s)
    audio_bytes = 0
    if not video.has_audio:
        audios = _audio_options(options)
        if audios:
            audio_bytes = _estimate_from_bitrate(audios[0], duration_s) or 0
    if video_bytes is None and duration_s:
        video_bytes = int(estimate_height_bps(height) / 8 * duration_s)
    if video_bytes is None:
        return None
    return video_bytes + audio_bytes


def resolve_plan(
    *,
    mode: str | None = "balanced",
    quality: str | None = None,
    proxy_height: int | None = None,
    source_height: int | None = None,
    max_height: int | None = None,
) -> IngestPlan:
    """Resolve a processing mode and explicit overrides into one plan.

    An explicit quality always wins over the mode default. ``proxy_height=0``
    disables the proxy, which is only sensible for small sources.
    """
    resolved_mode = (mode or "balanced").strip().lower()
    if resolved_mode not in PROCESSING_IDS:
        raise PolicyError(
            f"'{mode}' is not a processing mode. Choose fast, balanced, high or custom.",
            code="invalid_processing_mode",
        )
    if resolved_mode == CUSTOM_MODE:
        if not quality:
            raise PolicyError(
                "Custom processing needs an explicit download quality.", code="quality_required"
            )
        preset = {"quality": DEFAULT_QUALITY, "proxy_height": 480, "transcription_quality": "balanced",
                  "scene_fps": 1.0, "warnings": []}
    else:
        preset = PROCESSING_MODES[resolved_mode]

    resolved_quality = normalise_quality(quality or preset["quality"])
    if resolved_quality == "audio":
        if resolved_mode != CUSTOM_MODE:
            raise PolicyError(
                "Audio-only ingestion is only available in custom mode because it cannot export video.",
                code="audio_only_not_allowed",
            )
        resolved_proxy: int | None = None
    else:
        requested_proxy = proxy_height if proxy_height is not None else int(preset["proxy_height"])
        if requested_proxy and not (MIN_PROXY_HEIGHT <= requested_proxy <= MAX_PROXY_HEIGHT):
            raise PolicyError(
                f"A proxy height must be between {MIN_PROXY_HEIGHT}p and {MAX_PROXY_HEIGHT}p.",
                code="invalid_proxy_height",
            )
        resolved_proxy = requested_proxy or None

    warnings = list(preset.get("warnings") or [])
    if max_height and resolved_quality not in {"lowest", "best", "audio"}:
        cap = int(resolved_quality.rstrip("p"))
        if cap > max_height:
            warnings.append(
                f"The available source tops out near {max_height}p, below the requested {resolved_quality}."
            )
    download_proxy = bool(
        resolved_proxy
        and source_height
        and source_height > resolved_proxy + 16
    )
    if resolved_proxy and source_height and not download_proxy:
        warnings.append("The source is already small enough that a proxy would add nothing.")

    return IngestPlan(
        mode=resolved_mode,
        quality=resolved_quality,
        proxy_height=resolved_proxy,
        transcription_quality=str(preset["transcription_quality"]),
        scene_fps=float(preset["scene_fps"]),
        download_proxy=download_proxy,
        warnings=tuple(warnings),
    )


def estimate_display(choice: FormatChoice) -> dict:
    """Everything the UI needs to explain a download before it starts."""
    return {
        "quality": choice.quality,
        "requested_quality": choice.requested_quality,
        "width": choice.width,
        "height": choice.height,
        "fps": choice.fps,
        "video_codec": choice.video_codec,
        "audio_codec": choice.audio_codec,
        "container": choice.container,
        "has_video": choice.has_video,
        "estimated_bytes": choice.estimated_bytes,
        "approximate": True,
        "explanation": choice.explanation,
        "warnings": choice.warnings,
        "video_format_id": choice.video.format_id if choice.video else None,
        "audio_format_id": choice.audio.format_id if choice.audio else None,
    }
