"""Export-folder delivery: naming, no overwrite, project folders and reset."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from clipforge.errors import Conflict, ValidationFailed
from clipforge.pipeline.delivery import (
    delete_render_assets,
    display_path,
    export_settings_document,
    project_export_directory,
    save_render,
    validate_export_dir,
)
from clipforge.rendering.spec import build_spec
from clipforge.storage.local import LocalStorage


def _spec():
    return build_spec(
        source_id="src",
        segments=[{"start_ms": 0, "end_ms": 4_000}],
        aspect="9:16",
        resolution=720,
        captions_enabled=False,
    )


def _completed_render(repos, settings, project, *, name: str, data: bytes = b"x" * 4096):
    storage = LocalStorage(settings.data_dir)
    key = f"projects/{project['id']}/renders/{name}.mp4"
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    render = repos.renders.create(
        project_id=project["id"],
        job_id=None,
        candidate_id=None,
        spec=_spec().model_dump(),
        status="completed",
        display_name=f"{name}__v001.mp4",
        spec_hash=name,
        source_identity="source",
    )
    repos.renders.mark_completed(
        render["id"],
        relative_path=key,
        size_bytes=len(data),
        duration_ms=4_000,
        width=720,
        height=1280,
        checksum=hashlib.sha256(data).hexdigest(),
    )
    return repos.renders.get(render["id"]), storage


def test_default_project_folder_is_documents_clipforge_slug(tmp_path):
    from clipforge.config import Settings

    settings = Settings(
        data_dir=tmp_path / "data",
        host="127.0.0.1",
        port=8788,
        token="t",
        max_upload_bytes=1,
        max_duration_ms=1,
        min_free_bytes=0,
        whisper_model="tiny",
        whisper_device="cpu",
        whisper_compute_type="int8",
        llm_enabled=False,
        llm_base_url="",
        llm_model="",
        llm_api_key="",
        llm_timeout_seconds=1,
        export_dir=None,
    )
    project = {"name": "Customer Perspective", "settings": {}}

    assert settings.effective_export_dir == Path.home() / "Documents" / "Clipforge"
    assert project_export_directory(settings, project).name == "customer-perspective"


def test_project_override_changes_the_folder_and_reset_returns_to_default(settings, repos, project):
    # given
    target = settings.data_dir.parent / "custom-exports"
    repos.projects.set_export_dir(project["id"], str(target))
    project = repos.projects.get(project["id"])

    # then
    document = export_settings_document(settings, project)
    assert document["custom_dir"]
    assert document["effective_dir"].endswith("test-project")
    assert project_export_directory(settings, project) == target / "test-project"

    # when reset
    repos.projects.set_export_dir(project["id"], None)
    reset = export_settings_document(settings, repos.projects.get(project["id"]))
    assert reset["custom_dir"] is None


@pytest.mark.parametrize("value", ["relative/path", "/", ""])
def test_invalid_export_folders_are_refused(value):
    if value == "":
        assert validate_export_dir(value) is None
        return
    with pytest.raises(ValidationFailed):
        validate_export_dir(value)


def test_saving_copies_the_file_and_records_provenance(settings, repos, project):
    # given
    render, storage = _completed_render(repos, settings, project, name="render-one")

    # when
    saved = save_render(repos, settings, storage, render)

    # then
    target_dir = settings.effective_export_dir / "test-project"
    target = target_dir / saved["filename"]
    assert target.exists()
    assert target.read_bytes() == b"x" * 4096
    assert saved["already_present"] is False
    rows = repos.export_saves.list_for_render(render["id"])
    assert len(rows) == 1
    assert rows[0]["checksum"] == hashlib.sha256(b"x" * 4096).hexdigest()

    # and a repeated save is idempotent
    again = save_render(repos, settings, storage, render)
    assert again["already_present"] is True
    assert len(repos.export_saves.list_for_render(render["id"])) == 1


def test_a_different_file_with_the_same_name_gets_a_new_suffix(settings, repos, project):
    # given
    first, storage = _completed_render(repos, settings, project, name="clip")
    first_saved = save_render(repos, settings, storage, first)
    second, storage = _completed_render(repos, settings, project, name="clip", data=b"y" * 4096)

    # when
    second["display_name"] = first["display_name"]
    second_saved = save_render(repos, settings, storage, second)

    # then
    assert second_saved["filename"] != first_saved["filename"]
    assert second_saved["filename"].endswith("-2.mp4")


def test_saving_refuses_an_unfinished_render(settings, repos, project):
    render = repos.renders.create(
        project_id=project["id"],
        job_id=None,
        candidate_id=None,
        spec=_spec().model_dump(),
        status="running",
    )
    with pytest.raises(Conflict):
        save_render(repos, settings, LocalStorage(settings.data_dir), render)


def test_deleting_an_export_removes_app_storage_but_not_the_delivered_copy(settings, repos, project):
    # given
    render, storage = _completed_render(repos, settings, project, name="delete-me")
    saved = save_render(repos, settings, storage, render)
    delivered = settings.effective_export_dir / "test-project" / saved["filename"]
    stored = settings.data_dir / "projects" / project["id"] / "renders" / "delete-me.mp4"
    assert delivered.exists() and stored.exists()

    # when
    result = delete_render_assets(repos, storage, render)

    # then
    assert result["deleted"] is True
    assert not stored.exists()
    assert delivered.exists(), "an explicit app delete must not reach into the export folder"
    with pytest.raises(Exception):
        repos.renders.get(render["id"])


def test_display_path_hides_the_home_prefix(tmp_path):
    home = Path.home()
    assert display_path(home / "Documents" / "Clipforge") == str(
        Path("~") / "Documents" / "Clipforge"
    )
