from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from clipforge.api.deps import get_state
from clipforge.api.models import CaptionPresetBody, ProfileBody
from clipforge.branding.assets import audio_suffix, inspect_image
from clipforge.db.util import new_id
from clipforge.errors import NotFound, ValidationFailed
from clipforge.media.probe import probe
from clipforge.profiles.spec import ClientProfileDocument
from clipforge.storage.base import StorageObjectMissing, delete_keys, ensure_local
from clipforge.storage.workspace import ensure_space

router = APIRouter(prefix="/api", tags=["profiles"])

ASSET_DIR = "assets"


def _document(payload: ProfileBody) -> dict:
    try:
        return ClientProfileDocument.model_validate(payload.document).model_dump()
    except ValueError as exc:
        raise ValidationFailed(f"Profile is not valid: {exc}", code="invalid_profile") from exc


@router.get("/profiles")
def list_profiles(request: Request, include_archived: bool = False) -> dict:
    state = get_state(request)
    return {"profiles": state.repos.profiles.list(include_archived=include_archived)}


@router.post("/profiles")
def create_profile(payload: ProfileBody, request: Request) -> dict:
    state = get_state(request)
    return {"profile": state.repos.profiles.create(_document(payload))}


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request) -> dict:
    state = get_state(request)
    profile = state.repos.profiles.get(profile_id)
    return {
        "profile": profile,
        "assets": state.repos.assets.list_for_profile(profile_id),
    }


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileBody, request: Request) -> dict:
    state = get_state(request)
    return {"profile": state.repos.profiles.update(profile_id, _document(payload))}


@router.post("/profiles/{profile_id}/duplicate")
def duplicate_profile(profile_id: str, request: Request, name: str | None = None) -> dict:
    state = get_state(request)
    return {"profile": state.repos.profiles.duplicate(profile_id, name)}


@router.post("/profiles/{profile_id}/archive")
def archive_profile(profile_id: str, request: Request, archived: bool = True) -> dict:
    state = get_state(request)
    return {"profile": state.repos.profiles.set_archived(profile_id, archived)}


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request) -> dict:
    state = get_state(request)
    assets = state.repos.assets.list_for_profile(profile_id)
    delete_keys(state.storage, [asset.get("relative_path") for asset in assets])
    for asset in assets:
        if not asset.get("relative_path"):
            path = Path(asset.get("storage_path", ""))
            if path.exists():
                path.unlink()
    state.repos.profiles.delete(profile_id)
    return {"deleted": True, "profile_id": profile_id}


@router.get("/profiles/{profile_id}/assets")
def list_assets(profile_id: str, request: Request, kind: str | None = None) -> dict:
    state = get_state(request)
    state.repos.profiles.get(profile_id)
    return {"assets": state.repos.assets.list_for_profile(profile_id, kind)}


@router.post("/profiles/{profile_id}/assets")
async def upload_asset(
    profile_id: str,
    request: Request,
    kind: str = Form(...),
    licence_confirmed: bool = Form(False),
    file: UploadFile = File(...),
) -> dict:
    state = get_state(request)
    state.repos.profiles.get(profile_id)
    if kind not in {"logo", "music"}:
        raise ValidationFailed("Asset kind must be 'logo' or 'music'", code="invalid_asset_kind")

    data = await file.read(12 * 1024 * 1024 + 1)
    if len(data) > 12 * 1024 * 1024:
        raise ValidationFailed("That file is too large for a brand asset", code="asset_too_large")
    original = Path(file.filename or "asset").name[:200]
    ensure_space(state.settings, state.settings.data_dir / ASSET_DIR, required_bytes=2 * 1024 * 1024)

    if kind == "logo":
        info = inspect_image(data, original)
        suffix = info["suffix"]
        payload = info.get("data", data)
        width, height = info["width"], info["height"]
        duration_ms = None
        renderable = bool(info["renderable"])
        mime = info["mime"]
    else:
        if len(data) > 40 * 1024 * 1024:
            raise ValidationFailed("Music files must be 40 MB or smaller", code="asset_too_large")
        suffix = audio_suffix(original)
        payload = data
        width = height = None
        renderable = True
        mime = mimetypes.guess_type(f"x.{suffix}")[0] or "audio/mpeg"
        duration_ms = None

    asset_id = new_id()
    key = f"{ASSET_DIR}/{asset_id}.{suffix}"

    if kind == "music":
        temporary = state.settings.tmp_dir / f"asset-{asset_id}.{suffix}"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        try:
            info = probe(temporary)
            duration_ms = info.duration_ms
        except Exception:  # noqa: BLE001 - an unreadable probe is reported, not hidden
            duration_ms = None
        finally:
            temporary.unlink(missing_ok=True)

    state.storage.put_bytes(key, payload, content_type=mime)
    try:
        asset = state.repos.assets.create(
            profile_id=profile_id,
            project_id=None,
            kind=kind,
            original_name=original,
            storage_path=str(state.storage.path_for(key)),
            relative_path=key,
            mime=mime,
            width=width,
            height=height,
            size_bytes=len(payload),
            duration_ms=duration_ms,
            licence_confirmed=licence_confirmed if kind == "music" else False,
            renderable=renderable,
        )
    except Exception:
        state.storage.delete(key)
        raise
    return {"asset": asset}


@router.patch("/assets/{asset_id}")
def patch_asset(asset_id: str, request: Request, licence_confirmed: bool | None = None) -> dict:
    state = get_state(request)
    if licence_confirmed is None:
        raise ValidationFailed("Nothing to update", code="empty_patch")
    return {"asset": state.repos.assets.set_licence(asset_id, licence_confirmed)}


@router.get("/assets/{asset_id}/file")
def asset_file(asset_id: str, request: Request):
    state = get_state(request)
    asset = state.repos.assets.get(asset_id)
    try:
        path = ensure_local(
            state.storage,
            asset.get("relative_path"),
            legacy_path=asset.get("storage_path"),
            missing_message="That asset is no longer in storage",
        )
    except StorageObjectMissing as exc:
        raise NotFound(exc.message) from exc
    return FileResponse(path, media_type=asset["mime"], headers={"Cache-Control": "no-store"})


@router.delete("/assets/{asset_id}")
def delete_asset(asset_id: str, request: Request) -> dict:
    state = get_state(request)
    asset = state.repos.assets.get(asset_id)
    delete_keys(state.storage, [asset.get("relative_path")])
    if not asset.get("relative_path"):
        path = Path(asset.get("storage_path") or "")
        if path.exists():
            path.unlink()
    state.repos.assets.delete(asset_id)
    return {"deleted": True, "asset_id": asset_id}


@router.get("/caption-presets")
def list_caption_presets(request: Request) -> dict:
    state = get_state(request)
    return {"presets": state.repos.caption_presets.list()}


@router.post("/caption-presets")
def create_caption_preset(payload: CaptionPresetBody, request: Request) -> dict:
    state = get_state(request)
    return {"preset": state.repos.caption_presets.create(payload.name, payload.config)}


@router.delete("/caption-presets/{preset_id}")
def delete_caption_preset(preset_id: str, request: Request) -> dict:
    state = get_state(request)
    state.repos.caption_presets.delete(preset_id)
    return {"deleted": True, "preset_id": preset_id}
