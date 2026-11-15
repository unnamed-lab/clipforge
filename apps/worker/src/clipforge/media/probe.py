"""Media inspection through FFprobe."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from clipforge.errors import Unsupported
from clipforge.media.process import ProcessFailed, require, run

DECODABLE_VIDEO_CODECS = {
    "h264", "hevc", "h265", "vp8", "vp9", "av1", "mpeg4", "mpeg2video", "prores",
    "dnxhd", "theora", "mjpeg", "flv1", "wmv3", "vc1", "h263",
}
DECODABLE_AUDIO_CODECS = {
    "aac", "mp3", "opus", "vorbis", "ac3", "eac3", "flac", "pcm_s16le", "pcm_s24le",
    "pcm_f32le", "alac", "amr_nb", "wmav2", "mp2",
}


@dataclass
class MediaInfo:
    duration_ms: int
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool
    has_video: bool
    video_codec: str | None
    audio_codec: str | None
    audio_channels: int | None
    container: str | None
    bitrate: int | None
    size_bytes: int
    raw: dict

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("raw", None)
        return data


def _fraction(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_f = float(denominator)
            if denominator_f == 0:
                return None
            result = float(numerator) / denominator_f
        else:
            result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0 or result > 240:
        return None
    return round(result, 3)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            try:
                return int(float(side["rotation"]))
            except (TypeError, ValueError):
                return 0
    tags = stream.get("tags") or {}
    try:
        return int(float(tags.get("rotate", 0)))
    except (TypeError, ValueError):
        return 0


def probe(path: Path, *, timeout_seconds: float = 60) -> MediaInfo:
    ffprobe = require("ffprobe")
    if not path.exists():
        raise Unsupported("The uploaded file could not be found after saving it", code="upload_missing")
    try:
        result = run(
            [
                ffprobe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", "-show_entries",
                "format=duration,size,bit_rate,format_name:stream=index,codec_type,codec_name,width,height,"
                "avg_frame_rate,r_frame_rate,duration,channels,side_data_list:stream_tags=rotate",
                str(path),
            ],
            timeout_seconds=timeout_seconds,
        )
    except ProcessFailed as exc:
        raise Unsupported(
            "That file is not a video this app can read. Try MP4, MOV, MKV or WebM.",
            code="media_unreadable",
        ) from exc
    if result.returncode != 0:
        raise Unsupported(
            "That file is not a video this app can read. Try MP4, MOV, MKV or WebM.",
            code="media_unreadable",
        )

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise Unsupported("The media probe returned unreadable data", code="media_unreadable") from exc

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise Unsupported(
            "That file has no video stream. Audio-only files are not supported yet.",
            code="no_video_stream",
        )

    video_codec = (video.get("codec_name") or "").lower() or None
    if video_codec and video_codec not in DECODABLE_VIDEO_CODECS:
        raise Unsupported(f"The video codec ({video_codec}) is not supported", code="unsupported_codec")

    audio_codec = (audio.get("codec_name") or "").lower() or None
    if audio_codec and audio_codec not in DECODABLE_AUDIO_CODECS:
        raise Unsupported(f"The audio codec ({audio_codec}) is not supported", code="unsupported_codec")

    duration = 0.0
    for candidate in (fmt.get("duration"), video.get("duration"), audio.get("duration") if audio else None):
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            duration = value
            break
    if duration <= 0:
        raise Unsupported("The duration of that file could not be determined", code="invalid_duration")

    width = video.get("width")
    height = video.get("height")
    rotation = abs(_rotation(video)) % 360
    if rotation in {90, 270} and width and height:
        width, height = height, width

    fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))

    try:
        size_bytes = int(fmt.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size_bytes = path.stat().st_size if path.exists() else 0

    try:
        bitrate = int(fmt.get("bit_rate") or 0) or None
    except (TypeError, ValueError):
        bitrate = None

    return MediaInfo(
        duration_ms=int(round(duration * 1000)),
        width=int(width) if width else None,
        height=int(height) if height else None,
        fps=fps,
        has_audio=audio is not None,
        has_video=True,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_channels=int(audio["channels"]) if audio and audio.get("channels") else None,
        container=(fmt.get("format_name") or "").split(",")[0] or None,
        bitrate=bitrate,
        size_bytes=size_bytes,
        raw={"format": fmt, "streams": streams},
    )
