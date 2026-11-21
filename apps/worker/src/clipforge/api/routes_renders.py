from __future__ import annotations

import base64
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from clipforge.api.deps import get_state
from clipforge.api.models import CompositionPreviewRequest, RenderEstimateRequest, RenderRequest
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.media.assets import visual_input
from clipforge.profiles.spec import profile_snapshot, resolve_settings
from clipforge.rendering.composition import expand_composition
from clipforge.rendering.encode import EncodeSettings, resolve_encode
from clipforge.rendering.ffmpeg import build_preview_command, execute
from clipforge.rendering.spec import (
    ASPECT_DIMENSIONS,
    DEFAULT_CAPTION_PRESET,
    CompositionDecision,
    RenderSpec,
    build_spec,
)
from clipforge.security.paths import display_name
from clipforge.storage.base import StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout

router = APIRouter(prefix="/api", tags=["renders"])

MIN_RENDER_MS = 2_000
MAX_RENDER_MS = 15 * 60 * 1000


def _profile_document(settings: dict) -> dict:
    return {
        "branding": settings.get("branding") or {},
        "captions": settings.get("captions") or {},
        "audio": settings.get("audio") or {},
        "export": settings.get("export") or {},
        "brief": settings.get("brief") or {},
        "rules": settings.get("rules") or {},
    }


def _caption_overrides(settings: dict, payload: RenderRequest) -> dict:
    captions = settings["captions"]
    overrides = {
        "max_chars_per_line": captions.get("max_chars_per_line", 0),
        "shadow": captions.get("shadow", 0.0),
        "background_opacity": captions.get("background_opacity", 65),
        "highlight": "word" if captions.get("highlight_words", True) else "none",
    }
    for key in ("position", "position_pct", "alignment", "margin_v"):
        if key in captions:
            overrides[key] = captions[key]
    if payload.captions is not None:
        # Only fields the client actually sent may override the profile/preset.
        # Dumping defaults here would silently reset geometry the UI never touched.
        overrides.update(payload.captions.model_dump(exclude_unset=True))
    return overrides


def _audio_payload(settings: dict, payload: RenderRequest) -> dict:
    audio = settings["audio"]
    values = {
        "mode": audio.get("mode", "original"),
        "music_asset_id": audio.get("music_asset_id"),
        "music_gain_db": audio.get("music_gain_db", -18.0),
        "duck_music": audio.get("duck_music", True),
        "normalize": bool(audio.get("normalize", False) or payload.normalize_audio),
        "denoise": bool(audio.get("denoise", False)),
        "dialogue_enhance": bool(audio.get("dialogue_enhance", False)),
        "fade_in_ms": int(audio.get("fade_in_ms", 0)),
        "fade_out_ms": int(audio.get("fade_out_ms", 0)),
    }
    values.update(payload.audio or {})
    return values


def _logo_payload(settings: dict, payload: RenderRequest) -> tuple[dict, bool]:
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
    values.update(payload.logo or {})
    override = bool(values.pop("override_required", False))
    return values, override


def _editorial_summary(plan: dict, *, applied: bool, fallback: str = "") -> dict:
    decision = plan.get("editorial") or {}
    timeline = plan.get("timeline") or {}
    return {
        "strategy": decision.get("strategy"),
        "label": decision.get("label"),
        "rationale": decision.get("rationale"),
        "confidence": decision.get("confidence"),
        "reordered": bool(decision.get("reordered")),
        "applied": applied,
        "review_required": bool(decision.get("review_required")),
        "concerns": decision.get("concerns") or [],
        "fallback": fallback or decision.get("fallback") or "",
        "timeline_warnings": timeline.get("warnings") or [],
        "timeline": timeline,
    }


def _segments_for(
    payload: RenderRequest, candidate: dict, source: dict
) -> tuple[list[dict], dict | None, dict]:
    """Resolve the edit that will actually render.

    Manual trims win over any generated plan. A generated non-linear plan is used
    only when it is valid, and a plan marked for review is not applied silently:
    the caller must approve it or the chronological baseline is rendered.
    """
    source_duration = int(source["duration_ms"])
    detail = candidate.get("hook_detail") or {}
    hook = None
    if detail:
        hook = {
            "text": detail.get("text"),
            "start_ms": detail.get("start_ms"),
            "end_ms": detail.get("end_ms"),
            "source": detail.get("source"),
            "placement": detail.get("placement"),
            "direction": detail.get("direction"),
            "approved": detail.get("approved"),
        }

    chronological = [{"start_ms": candidate["start_ms"], "end_ms": candidate["end_ms"], "role": "main"}]
    plan = candidate.get("editorial") or {}
    decision = plan.get("editorial") or {}
    timeline = plan.get("timeline") or {}
    fallback = ""

    if candidate.get("edited"):
        fallback = "manual_trim"
    elif not payload.apply_editorial:
        fallback = "editorial_disabled"
    elif not timeline.get("valid", False) or not timeline.get("entries"):
        fallback = "timeline_unavailable"
    elif decision.get("reordered") and decision.get("review_required") and not payload.approve_editorial:
        fallback = "review_not_approved"

    if not fallback:
        segments = []
        for entry in timeline.get("entries") or []:
            role = entry.get("role", "main")
            if role == "hook" and not payload.include_hook:
                continue
            start = int(entry.get("source_start_ms", -1))
            end = int(entry.get("source_end_ms", -1))
            if start < 0 or end <= start or end > source_duration:
                fallback = "invalid_timeline_span"
                break
            segments.append({"start_ms": start, "end_ms": end, "role": role})
        if not fallback and segments:
            return segments, hook, _editorial_summary(plan, applied=True)

    segments = list(chronological)
    if (
        payload.include_hook
        and hook
        and hook.get("approved")
        and hook.get("placement") == "before"
        # Only a line that sits before the clip can open it without repeating or
        # jumping forward. A later line is a cold open and belongs to the timeline.
        and hook.get("direction") == "before"
        and isinstance(hook.get("start_ms"), int)
    ):
        start, end = int(hook["start_ms"]), int(hook["end_ms"])
        if 0 <= start < end <= int(candidate["start_ms"]):
            segments.insert(0, {"start_ms": start, "end_ms": end, "role": "hook"})
    return segments, hook, _editorial_summary(plan, applied=False, fallback=fallback or "chronological")


def _checks(settings: dict, captions_enabled: bool, logo: dict, audio: dict, override: bool,
            repos) -> None:
    branding, captions = settings["branding"], settings["captions"]
    if branding.get("logo_required") and not logo.get("enabled") and not override:
        raise ValidationFailed(
            "This client profile requires a logo. Upload one or explicitly override the requirement.",
            code="logo_required",
        )
    if captions.get("required") and not captions_enabled and not override:
        raise ValidationFailed(
            "This client profile requires captions. Enable them or explicitly override the requirement.",
            code="captions_required",
        )
    if logo.get("enabled"):
        asset_id = logo.get("asset_id")
        if not asset_id:
            raise ValidationFailed("Choose a logo asset for this export", code="logo_missing")
        asset = repos.assets.get(asset_id)
        if not asset.get("renderable") and not override:
            raise ValidationFailed(
                "That logo is an SVG, which FFmpeg cannot overlay. Upload a transparent PNG "
                "or explicitly override the requirement.",
                code="logo_not_renderable",
            )
    if audio.get("mode") not in {"original", None}:
        if audio.get("mode") == "replace" and not settings["audio"].get("allow_replacement"):
            raise ValidationFailed(
                "This profile does not permit replacing the original audio.", code="audio_replacement_not_permitted"
            )
        asset_id = audio.get("music_asset_id")
        if not asset_id:
            raise ValidationFailed("Choose an approved music file for this audio mode", code="music_missing")
        asset = repos.assets.get(asset_id)
        if not asset.get("licence_confirmed"):
            raise ValidationFailed(
                "The selected music is not confirmed as licensed for this use. Confirm its licence first.",
                code="music_unlicensed",
            )


def _resolve_compression(raw: object) -> EncodeSettings:
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


def _compression_payload(settings: dict, payload: RenderRequest, explicit: set[str]) -> EncodeSettings:
    """Resolve the compression choice, honouring a project-level default.

    An explicit request always wins; otherwise the profile's saved export
    preferences are used, and only then the balanced preset.
    """
    raw = payload.compression if "compression" in explicit else (settings.get("export") or {}).get("compression")
    return _resolve_compression(raw)


def _layout_payload(settings: dict, payload: RenderRequest) -> dict:
    """Resolve the effective layout from the request, then project defaults.

    Both the export and the composition preview read this one function, so a
    preset saved on the project cannot mean one thing in the UI and another in
    the encoder.
    """
    export = settings["export"]
    explicit = payload.model_fields_set
    face_cam_rect = (
        payload.face_cam_rect if "face_cam_rect" in explicit else export.get("face_cam_rect")
    )
    if hasattr(face_cam_rect, "model_dump"):
        face_cam_rect = face_cam_rect.model_dump()
    return {
        "aspect": payload.aspect if "aspect" in explicit else export.get("aspect", payload.aspect),
        "resolution": (
            payload.resolution if "resolution" in explicit
            else int(export.get("resolution", payload.resolution))
        ),
        "framing": (
            payload.framing if "framing" in explicit
            else settings["branding"].get("framing", payload.framing)
        ),
        "fps": payload.fps if "fps" in explicit else export.get("fps"),
        "composition": (
            payload.composition if "composition" in explicit
            else export.get("composition", "auto")
        ),
        "face_cam_position": (
            payload.face_cam_position if "face_cam_position" in explicit
            else export.get("face_cam_position", "bottom")
        ),
        "face_cam_rect": face_cam_rect,
        "face_cam_overrides": (
            [item.model_dump() for item in payload.face_cam_overrides]
            if "face_cam_overrides" in explicit
            else list(export.get("face_cam_overrides") or [])
        ),
        "stack_split_pct": (
            payload.stack_split_pct if "stack_split_pct" in explicit
            else float(export.get("stack_split_pct", 60.0))
        ),
        "adaptive_stack": (
            payload.adaptive_stack if "adaptive_stack" in explicit
            else bool(export.get("adaptive_stack", False))
        ),
        "crop_x_pct": payload.crop_x_pct if "crop_x_pct" in explicit else export.get("crop_x_pct"),
        "crop_y_pct": payload.crop_y_pct if "crop_y_pct" in explicit else export.get("crop_y_pct"),
    }


@router.post("/projects/{project_id}/renders/estimate")
def estimate_render(project_id: str, payload: RenderEstimateRequest, request: Request) -> dict:
    """Approximate the output size from duration and the chosen bitrate.

    The estimate is labelled approximate on purpose: FFmpeg's real output
    depends on the content. It exists so the user can compare presets, not so
    the UI can promise a file size.
    """
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    settings = resolve_settings(profile, project.get("overrides"))["settings"]
    raw = payload.compression if "compression" in payload.model_fields_set else None
    compression = _resolve_compression(
        raw if raw is not None else (settings.get("export") or {}).get("compression")
    )
    base = ASPECT_DIMENSIONS[payload.aspect][int(payload.resolution)]
    width, height = compression.dimensions_for(base)
    fps = min(float(payload.fps or compression.max_fps or 30), 60.0)
    estimated = compression.estimate_bytes(dimensions=base, fps=fps, duration_ms=payload.duration_ms)
    return {
        "estimated_bytes": estimated,
        "approximate": True,
        "basis": "duration × estimated video bitrate + audio bitrate",
        "width": width,
        "height": height,
        "fps": fps,
        "encode": compression.to_dict(),
    }


@router.post("/projects/{project_id}/renders")
def create_render(project_id: str, payload: RenderRequest, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before rendering", code="source_missing")

    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    settings = resolved["settings"]
    snapshot = profile_snapshot(profile)
    source_duration = int(source["duration_ms"])

    if payload.edit_spec_id:
        saved = state.repos.edit_specs.get(payload.edit_spec_id)
        if saved["project_id"] != project_id:
            raise NotFound("That edit belongs to a different project")
        try:
            spec = RenderSpec.model_validate(saved["spec"])
            spec.validate_against(source_duration)
        except ValueError as exc:
            raise ValidationFailed(str(exc), code="invalid_render_spec") from exc
        spec.edit_spec_id = saved["id"]
        if "compression" in payload.model_fields_set:
            spec.encode = _compression_payload(settings, payload, payload.model_fields_set)
        if not spec.profile_snapshot:
            spec.profile_snapshot = saved["profile_snapshot"]
        render = state.repos.renders.create(
            project_id=project_id, job_id=None, candidate_id=saved.get("candidate_id"),
            spec=spec.model_dump(), kind="export", profile_snapshot=spec.profile_snapshot,
            edit_spec_id=saved["id"],
        )
        job = state.queue.enqueue(project_id=project_id, job_type="render", payload={"render_id": render["id"]})
        state.repos.renders.attach_job(render["id"], job["id"])
        state.repos.projects.touch(project_id)
        return {"render": state.repos.renders.get(render["id"]), "job": job}

    candidate_id = None
    hook = None
    editorial: dict = {}
    duration: dict = {}
    if payload.candidate_id:
        candidate = state.repos.candidates.get(project_id, payload.candidate_id)
        if candidate["decision"] == "rejected":
            raise Conflict("That candidate was rejected. Restore it before rendering.")
        candidate_id = candidate["id"]
        segments, hook, editorial = _segments_for(payload, candidate, source)
        duration = candidate.get("duration") or {}
        title = payload.title or candidate.get("title")
    elif payload.segments:
        segments = []
        for raw in payload.segments:
            role = raw.get("role", "main")
            if role not in {"hook", "context", "main", "development", "payoff", "loop"}:
                raise ValidationFailed("Segment role is not a recognised narrative role", code="invalid_segment")
            segments.append({"start_ms": int(raw.get("start_ms", 0)), "end_ms": int(raw.get("end_ms", 0)),
                             "role": role})
        title = payload.title
    else:
        raise ValidationFailed("No segments were provided", code="invalid_segment")

    total = sum(segment["end_ms"] - segment["start_ms"] for segment in segments)
    if total < MIN_RENDER_MS:
        raise ValidationFailed("The planned output is shorter than 2 seconds", code="invalid_segment")
    if total > MAX_RENDER_MS:
        raise ValidationFailed("The planned output is longer than 15 minutes", code="invalid_segment")

    layout = _layout_payload(settings, payload)
    aspect = layout["aspect"]
    resolution = layout["resolution"]
    framing = layout["framing"]
    fps = layout["fps"]
    composition = layout["composition"]
    face_cam_position = layout["face_cam_position"]
    face_cam_rect = layout["face_cam_rect"]
    face_cam_overrides = layout["face_cam_overrides"]
    stack_split_pct = layout["stack_split_pct"]
    adaptive_stack = layout["adaptive_stack"]
    crop_x_pct = layout["crop_x_pct"]
    crop_y_pct = layout["crop_y_pct"]
    explicit = payload.model_fields_set
    caption_preset = payload.caption_preset if "caption_preset" in explicit else settings["captions"].get(
        "preset", DEFAULT_CAPTION_PRESET
    )
    captions_enabled = payload.captions_enabled if "captions_enabled" in explicit else bool(
        settings["captions"].get("enabled", True)
    )
    audio = _audio_payload(settings, payload)
    logo, logo_override = _logo_payload(settings, payload)
    compression = _compression_payload(settings, payload, explicit)
    override = bool(payload.override_policy or logo_override)
    _checks(settings, captions_enabled, logo, audio, override, state.repos)

    try:
        spec = build_spec(
            source_id=source["id"],
            segments=segments,
            aspect=aspect,
            resolution=resolution,
            framing=framing,
            composition=composition,
            compression=compression,
            crop_x_pct=crop_x_pct,
            crop_y_pct=crop_y_pct,
            face_cam_position=face_cam_position,
            face_cam_rect=face_cam_rect,
            face_cam_overrides=face_cam_overrides,
            stack_split_pct=stack_split_pct,
            adaptive_stack=adaptive_stack,
            caption_preset=caption_preset,
            captions_enabled=captions_enabled,
            caption_overrides=_caption_overrides(settings, payload),
            captions_burn_in=payload.captions_burn_in,
            captions_sidecar=payload.captions_sidecar,
            audio=audio,
            logo=logo,
            fps=fps,
            title=title,
            hook=hook,
            editorial=editorial,
            duration=duration,
            chronological=bool(settings["brief"].get("hook", {}).get("strict_chronological", False)),
            profile={"id": (profile or {}).get("id"), "name": (profile or {}).get("name"),
                     "version": (profile or {}).get("version")},
            profile_snapshot=snapshot,
            edit_spec_id=None,
        )
        for key, raw in (payload.composition_overrides or {}).items():
            try:
                index = int(key)
                decision = CompositionDecision.model_validate(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationFailed("A composition override is invalid", code="invalid_composition") from exc
            if index < 0 or index >= len(spec.segments):
                raise ValidationFailed(
                    f"Composition override {key} does not match a segment", code="invalid_composition"
                )
            decision.auto = False
            spec.segments[index].composition = decision
        spec.validate_against(source_duration)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_render_spec") from exc

    if override and (
        (settings["branding"].get("logo_required") and not logo.get("enabled"))
        or (settings["captions"].get("required") and not captions_enabled)
    ):
        spec.profile_snapshot = {
            **snapshot,
            "policy_override": {
                "logo_required_waived": bool(settings["branding"].get("logo_required") and not logo.get("enabled")),
                "captions_required_waived": bool(settings["captions"].get("required") and not captions_enabled),
            },
        }

    render = state.repos.renders.create(
        project_id=project_id,
        job_id=None,
        candidate_id=candidate_id,
        spec=spec.model_dump(),
        kind="export",
        profile_snapshot=spec.profile_snapshot,
        edit_spec_id=None,
    )
    job = state.queue.enqueue(
        project_id=project_id,
        job_type="render",
        payload={"render_id": render["id"]},
    )
    state.repos.renders.attach_job(render["id"], job["id"])
    state.repos.projects.touch(project_id)
    return {"render": state.repos.renders.get(render["id"]), "job": job}


@router.post("/projects/{project_id}/renders/composition-preview")
def composition_preview(
    project_id: str, payload: CompositionPreviewRequest, request: Request
):
    """Return one real frame rendered through the export composition chain.

    This is deliberately a preview of the encoder, not a diagram of it. The
    frame is produced by the same framing filters ``build_command`` uses, so a
    layout can only be advertised when FFmpeg can actually draw it.
    """
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    source = state.repos.sources.find_optional(project_id)
    if source is None:
        raise ValidationFailed("Import a video before previewing a layout", code="source_missing")
    if not source.get("has_video"):
        raise ValidationFailed(
            "This source was downloaded without video, so there is no frame to preview.",
            code="no_video_stream",
        )

    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    settings = resolve_settings(profile, project.get("overrides"))["settings"]
    source_duration = int(source["duration_ms"])

    if payload.candidate_id:
        candidate = state.repos.candidates.get(project_id, payload.candidate_id)
        segments, _, _ = _segments_for(payload, candidate, source)
        title = payload.title or candidate.get("title")
    elif payload.segments:
        segments = []
        for raw in payload.segments:
            role = raw.get("role", "main")
            if role not in {"hook", "context", "main", "development", "payoff", "loop"}:
                raise ValidationFailed("Segment role is not a recognised narrative role", code="invalid_segment")
            segments.append(
                {"start_ms": int(raw.get("start_ms", 0)), "end_ms": int(raw.get("end_ms", 0)), "role": role}
            )
        title = payload.title
    else:
        raise ValidationFailed("No segments were provided", code="invalid_segment")

    layout = _layout_payload(settings, payload)
    try:
        spec = build_spec(
            source_id=source["id"],
            segments=segments,
            aspect=layout["aspect"],
            resolution=layout["resolution"],
            framing=layout["framing"],
            composition=layout["composition"],
            compression=resolve_encode(None),
            crop_x_pct=layout["crop_x_pct"],
            crop_y_pct=layout["crop_y_pct"],
            face_cam_position=layout["face_cam_position"],
            face_cam_rect=layout["face_cam_rect"],
            face_cam_overrides=layout["face_cam_overrides"],
            stack_split_pct=layout["stack_split_pct"],
            adaptive_stack=layout["adaptive_stack"],
            captions_enabled=False,
            captions_burn_in=False,
            captions_sidecar=False,
            fps=layout["fps"],
            title=title,
        )
        spec.validate_against(source_duration)
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="invalid_render_spec") from exc

    try:
        source_path = ensure_local(
            state.storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The original video file is missing from this project.",
        )
    except StorageObjectMissing as exc:
        raise ValidationFailed(exc.message, code="source_missing") from exc

    paths = ensure_layout(state.settings, project_id)
    download = source.get("download") or {}
    scene_fps = (download.get("processing") or {}).get("scene_fps")
    analysis_path, analysis_height = visual_input(state.repos, state.storage, source, source_path)
    plan = expand_composition(
        spec,
        analysis_path,
        source.get("width"),
        source.get("height"),
        source_duration_ms=source_duration,
        cache_dir=paths / "analysis" / "scenes",
        scene_fps=scene_fps,
        analysis_height=analysis_height,
    )
    spec.segments = plan.segments
    spec.facecam_track = plan.facecam_track

    selected = None
    if payload.preview_at_ms is not None:
        for segment in spec.segments:
            if segment.start_ms <= payload.preview_at_ms < segment.end_ms:
                selected = segment
                break
    if selected is None:
        selected = spec.segments[0]
    decision = selected.composition

    fingerprint = hashlib.sha1(
        json.dumps(
            {
                "segments": [
                    (
                        segment.start_ms,
                        segment.end_ms,
                        segment.composition.model_dump(mode="json") if segment.composition else None,
                    )
                    for segment in spec.segments
                ],
                "video": spec.video.model_dump(mode="json"),
                "facecam_track": (
                    spec.facecam_track.model_dump(mode="json") if spec.facecam_track else None
                ),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    work = paths / "tmp" / f"preview-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        command = build_preview_command(
            spec,
            segment=selected,
            source=source_path,
            cwd=work,
            at_ms=payload.preview_at_ms,
            source_width=source.get("width"),
            source_height=source.get("height"),
        )
        try:
            execute(command, timeout_seconds=120)
        except (TimeoutError, RuntimeError) as exc:
            raise ValidationFailed(
                "The preview frame could not be rendered. The source file may be unreadable.",
                code="preview_failed",
            ) from exc
        frame_path = work / command.output_name
        if not frame_path.exists() or frame_path.stat().st_size < 512:
            raise ValidationFailed("The preview frame was empty.", code="preview_failed")
        encoded = base64.b64encode(frame_path.read_bytes()).decode("ascii")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    requested = layout["composition"]
    fallback = bool(
        decision
        and (
            (requested == "stack" and decision.mode != "stack")
            or (requested == "facecam_focus" and decision.source_rect is None)
        )
    )
    width, height = spec.output_dimensions
    warnings = list(dict.fromkeys([*plan.warnings, *(decision.warnings if decision else [])]))
    preview_midpoint = selected.start_ms + (selected.end_ms - selected.start_ms) // 2
    preview_range = None
    if spec.facecam_track:
        for item in spec.facecam_track.ranges:
            if item.start_ms <= preview_midpoint < item.end_ms:
                preview_range = item
                break
    analysis = plan.analyses[0].analysis if plan.analyses else {}
    return JSONResponse(
        content={
            "image": f"data:image/jpeg;base64,{encoded}",
            "width": width,
            "height": height,
            "fingerprint": fingerprint,
            "composition": requested,
            "face_cam_position": layout["face_cam_position"],
            "stack_split_pct": layout["stack_split_pct"],
            "segment": {
                "start_ms": selected.start_ms,
                "end_ms": selected.end_ms,
                "duration_ms": selected.duration_ms,
            },
            "decision": decision.model_dump(mode="json") if decision else None,
            "facecam": {
                "detected": bool(decision and decision.facecam is not None),
                "rect": decision.facecam.model_dump() if decision and decision.facecam else None,
                "state": decision.facecam_state if decision else None,
                "confidence": preview_range.confidence if preview_range else None,
                "reason": preview_range.reason if preview_range else None,
                "review_required": bool(
                    preview_range
                    and (
                        preview_range.state == "uncertain"
                        or (not decision and preview_range.rect is not None)
                    )
                ),
                "detector": analysis.get("facecam_detector"),
                "detector_available": analysis.get("facecam_detector_available"),
                "analysis_height": analysis.get("facecam_analysis_height"),
                "ranges": (
                    [item.model_dump(mode="json") for item in spec.facecam_track.ranges]
                    if spec.facecam_track else []
                ),
                "observations": (
                    [item.model_dump(mode="json") for item in spec.facecam_track.observations]
                    if spec.facecam_track else []
                ),
                "warnings": (
                    list(spec.facecam_track.warnings) if spec.facecam_track else []
                ),
            },
            "gameplay_rect": (
                decision.gameplay_rect.model_dump() if decision and decision.gameplay_rect else None
            ),
            "facecam_panel_rect": (
                decision.facecam_panel_rect.model_dump()
                if decision and decision.facecam_panel_rect else None
            ),
            "fallback": fallback,
            "warnings": warnings,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/projects/{project_id}/renders")
def list_renders(project_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.projects.get(project_id)
    return {"renders": state.repos.renders.list_for_project(project_id)}


@router.get("/renders/{render_id}")
def get_render(render_id: str, request: Request) -> dict:
    state = get_state(request)
    return {"render": state.repos.renders.get(render_id)}


def _ready_path(render_id: str, request: Request) -> tuple[Path, dict]:
    state = get_state(request)
    raw = state.repos.renders.get_raw(render_id)
    if raw["status"] != "completed" or not raw.get("relative_path"):
        raise NotFound("That render is not ready yet")
    try:
        path = ensure_local(
            state.storage,
            raw["relative_path"],
            missing_message="The rendered file is no longer in storage",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc
    return path, raw


@router.get("/renders/{render_id}/download")
def download_render(render_id: str, request: Request):
    path, raw = _ready_path(render_id, request)
    state = get_state(request)
    project = state.repos.projects.get(raw["project_id"])
    spec = RenderSpec.model_validate(json.loads(raw["spec_json"] or "{}"))
    label = spec.title or "clip"
    filename = display_name(f"{project['name']} - {label}", fallback="clip")
    if not filename.lower().endswith(".mp4"):
        filename += ".mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename, headers={"Cache-Control": "no-store"})


@router.get("/renders/{render_id}/draft")
def download_render_draft(render_id: str, request: Request):
    state = get_state(request)
    raw = state.repos.renders.get_raw(render_id)
    review = json.loads(raw.get("review_json") or "{}")
    relative_path = review.get("draft_relative_path")
    if not relative_path:
        raise NotFound("That render has no preserved draft")
    try:
        path = ensure_local(
            state.storage,
            relative_path,
            missing_message="The render draft is no longer in storage",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{render_id}.draft.mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/renders/{render_id}/stream")
def stream_render(render_id: str, request: Request):
    path, _ = _ready_path(render_id, request)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.get("/renders/{render_id}/subtitles")
def download_subtitles(render_id: str, request: Request):
    state = get_state(request)
    raw = state.repos.renders.get_raw(render_id)
    if not raw.get("subtitle_path"):
        raise NotFound("That render has no sidecar subtitles")
    try:
        path = ensure_local(
            state.storage,
            raw["subtitle_path"],
            missing_message="The subtitle file is no longer in storage",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc
    return FileResponse(path, media_type="application/x-subrip", filename=f"{render_id}.srt",
                        headers={"Cache-Control": "no-store"})
