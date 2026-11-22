"""One place that resolves request + profile + project defaults.

``preview``, ``composition preview``, the HTTP export route and the autonomous
orchestrator all ask these functions. Restating the precedence anywhere else is
how a profile default starts meaning two different things.
"""

from __future__ import annotations

from typing import Any

from clipforge.rendering.encode import EncodeSettings, resolve_encode


def profile_document(settings: dict) -> dict:
    return {
        "branding": settings.get("branding") or {},
        "captions": settings.get("captions") or {},
        "audio": settings.get("audio") or {},
        "export": settings.get("export") or {},
        "brief": settings.get("brief") or {},
        "rules": settings.get("rules") or {},
    }


def caption_overrides(settings: dict, *, captions: dict | None,
                      captions_explicit: bool) -> dict:
    policy = settings["captions"]
    overrides = {
        "max_chars_per_line": policy.get("max_chars_per_line", 0),
        "shadow": policy.get("shadow", 0.0),
        "background_opacity": policy.get("background_opacity", 65),
        "highlight": "word" if policy.get("highlight_words", True) else "none",
    }
    for key in ("position", "position_pct", "alignment", "margin_v"):
        if key in policy:
            overrides[key] = policy[key]
    if captions_explicit and captions:
        # Only fields the client actually sent may override the profile/preset.
        overrides.update(captions)
    return overrides


def audio_payload(settings: dict, *, audio: dict | None, normalize_audio: bool) -> dict:
    policy = settings["audio"]
    values = {
        "mode": policy.get("mode", "original"),
        "music_asset_id": policy.get("music_asset_id"),
        "music_gain_db": policy.get("music_gain_db", -18.0),
        "duck_music": policy.get("duck_music", True),
        "normalize": bool(policy.get("normalize", False) or normalize_audio),
        "denoise": bool(policy.get("denoise", False)),
        "dialogue_enhance": bool(policy.get("dialogue_enhance", False)),
        "fade_in_ms": int(policy.get("fade_in_ms", 0)),
        "fade_out_ms": int(policy.get("fade_out_ms", 0)),
    }
    values.update(audio or {})
    return values


def logo_payload(settings: dict, *, logo: dict | None) -> tuple[dict, bool]:
    branding = settings["branding"]
    asset_id = branding.get("logo_asset_id")
    values = {
        "enabled": bool(asset_id),
        "required": bool(branding.get("logo_required", False)),
        "asset_id": asset_id,
        "position": branding.get("logo_position", "top-right"),
        "width_pct": branding.get("logo_width_pct", 18.0),
        "opacity": branding.get("logo_opacity", 1.0),
        "margin_px": branding.get("logo_margin_px", 48),
        "custom_x_pct": branding.get("logo_custom_x_pct", 82.0),
        "custom_y_pct": branding.get("logo_custom_y_pct", 8.0),
        "from_ms": branding.get("logo_from_ms"),
        "to_ms": branding.get("logo_to_ms"),
    }
    values.update(logo or {})
    override = bool(values.pop("override_required", False))
    return values, override


def layout_payload(settings: dict, requested: dict, explicit: set[str]) -> dict:
    """Resolve the effective layout from the request, then project defaults."""
    export = settings["export"]
    face_cam_rect = (
        requested.get("face_cam_rect") if "face_cam_rect" in explicit
        else export.get("face_cam_rect")
    )
    if hasattr(face_cam_rect, "model_dump"):
        face_cam_rect = face_cam_rect.model_dump()
    if "face_cam_overrides" in explicit:
        overrides = [
            item.model_dump() if hasattr(item, "model_dump") else item
            for item in (requested.get("face_cam_overrides") or [])
        ]
    else:
        overrides = list(export.get("face_cam_overrides") or [])
    return {
        "aspect": (
            requested.get("aspect") if "aspect" in explicit
            else export.get("aspect", requested.get("aspect", "9:16"))
        ),
        "resolution": int(
            requested.get("resolution") if "resolution" in explicit
            else export.get("resolution", requested.get("resolution", 1080))
        ),
        "resolution_mode": (
            requested.get("resolution_mode") if "resolution_mode" in explicit
            else export.get("resolution_mode", "class")
        ),
        "custom_width": (
            requested.get("custom_width") if "custom_width" in explicit
            else export.get("custom_width")
        ),
        "custom_height": (
            requested.get("custom_height") if "custom_height" in explicit
            else export.get("custom_height")
        ),
        "enhancement": (
            requested.get("enhancement")
            if requested.get("enhancement") is not None
            else export.get("enhancement")
        ),
        "framing": (
            requested.get("framing") if "framing" in explicit
            else settings["branding"].get("framing", requested.get("framing", "crop"))
        ),
        "fps": requested.get("fps") if "fps" in explicit else export.get("fps"),
        "composition": (
            requested.get("composition") if "composition" in explicit
            else export.get("composition", "auto")
        ),
        "face_cam_position": (
            requested.get("face_cam_position") if "face_cam_position" in explicit
            else export.get("face_cam_position", "bottom")
        ),
        "face_cam_rect": face_cam_rect,
        "face_cam_overrides": overrides,
        "stack_split_pct": float(
            requested.get("stack_split_pct") if "stack_split_pct" in explicit
            else export.get("stack_split_pct", 60.0)
        ),
        "adaptive_stack": (
            bool(requested.get("adaptive_stack")) if "adaptive_stack" in explicit
            else bool(export.get("adaptive_stack", False))
        ),
        "crop_x_pct": (
            requested.get("crop_x_pct") if "crop_x_pct" in explicit
            else export.get("crop_x_pct")
        ),
        "crop_y_pct": (
            requested.get("crop_y_pct") if "crop_y_pct" in explicit
            else export.get("crop_y_pct")
        ),
    }


def resolve_compression(raw: object) -> EncodeSettings:
    from clipforge.errors import ValidationFailed

    if raw is None:
        return resolve_encode(None)
    if isinstance(raw, str):
        try:
            return resolve_encode(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="invalid_compression") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed("Compression settings must be an object", code="invalid_compression")
    custom = {key: value for key, value in raw.items() if key != "preset"}
    try:
        return resolve_encode(raw.get("preset"), custom or None)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_compression") from exc


def compression_payload(settings: dict, *, compression: Any, explicit: bool) -> EncodeSettings:
    """An explicit request wins; otherwise the profile default; then balanced."""
    raw = compression if explicit else (settings.get("export") or {}).get("compression")
    return resolve_compression(raw)


def caption_settings(settings: dict, *, requested: dict, explicit: set[str],
                     default_preset: str) -> tuple[str, bool]:
    preset = (
        requested.get("caption_preset") if "caption_preset" in explicit
        else settings["captions"].get("preset", default_preset)
    )
    enabled = (
        requested.get("captions_enabled") if "captions_enabled" in explicit
        else bool(settings["captions"].get("enabled", True))
    )
    return str(preset), bool(enabled)
