from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from clipforge.api.deps import get_state
from clipforge.api.models import RenderRequest
from clipforge.errors import Conflict, NotFound, ValidationFailed
from clipforge.profiles.spec import profile_snapshot, resolve_settings
from clipforge.rendering.spec import CompositionDecision, RenderSpec, build_spec
from clipforge.security.paths import display_name
from clipforge.storage.base import StorageObjectMissing, ensure_local

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
        and isinstance(hook.get("start_ms"), int)
    ):
        start, end = int(hook["start_ms"]), int(hook["end_ms"])
        if 0 <= start < end <= source_duration:
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

    export = settings["export"]
    explicit = payload.model_fields_set
    aspect = payload.aspect if "aspect" in explicit else export.get("aspect", payload.aspect)
    resolution = payload.resolution if "resolution" in explicit else int(export.get("resolution", payload.resolution))
    framing = payload.framing if "framing" in explicit else settings["branding"].get("framing", payload.framing)
    fps = payload.fps if "fps" in explicit else export.get("fps")
    caption_preset = payload.caption_preset if "caption_preset" in explicit else settings["captions"].get("preset", "clean")
    captions_enabled = payload.captions_enabled if "captions_enabled" in explicit else bool(
        settings["captions"].get("enabled", True)
    )
    audio = _audio_payload(settings, payload)
    logo, logo_override = _logo_payload(settings, payload)
    override = bool(payload.override_policy or logo_override)
    _checks(settings, captions_enabled, logo, audio, override, state.repos)

    try:
        spec = build_spec(
            source_id=source["id"],
            segments=segments,
            aspect=aspect,
            resolution=resolution,
            framing=framing,
            composition=payload.composition if "composition" in explicit else "auto",
            crop_x_pct=payload.crop_x_pct,
            crop_y_pct=payload.crop_y_pct,
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
