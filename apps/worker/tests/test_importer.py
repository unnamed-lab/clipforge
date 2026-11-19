"""Source ingestion: format selection, retries, cancellation, reuse and assets.

The extractor is faked here. These tests exercise the decisions *around* the
download — what gets retried, what gets cleaned up, what gets reused — without
touching the network, FFmpeg or a real media file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from clipforge.errors import JobFailed
from clipforge.media import importer
from clipforge.media.probe import MediaInfo
from clipforge.media.process import ProcessCancelled
from clipforge.media.importer import (
    _download_summary,
    _existing_reusable_source,
    _map_error,
    cleanup_partials,
    download_remote,
    persist_source,
    progress_band,
)
from clipforge.media.policy import choose_format
from clipforge.security.urls import ValidatedUrl
from clipforge.storage.local import LocalStorage
from clipforge.storage.workspace import ensure_layout

URL = "https://example.com/watch?v=abc"

INFO = {
    "title": "A talk about hiring",
    "uploader": "Someone",
    "duration": 120.0,
    "thumbnail": "https://example.com/t.jpg",
    "formats": [
        {
            "format_id": "134",
            "height": 360,
            "width": 640,
            "fps": 30,
            "vcodec": "avc1.4d401e",
            "acodec": "none",
            "ext": "mp4",
            "tbr": 500,
            "filesize": 6_000_000,
        },
        {
            "format_id": "136",
            "height": 720,
            "width": 1280,
            "fps": 30,
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "ext": "mp4",
            "tbr": 1_800,
            "filesize": 20_000_000,
        },
        {
            "format_id": "140",
            "height": None,
            "width": None,
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "ext": "m4a",
            "abr": 128,
            "filesize": 2_000_000,
        },
    ],
}


def _validated(url: str = URL) -> ValidatedUrl:
    return ValidatedUrl(original=url, resolved=url, host="example.com", is_known_provider=False)


def _media_info(*, has_video: bool = True, height: int | None = 720) -> MediaInfo:
    return MediaInfo(
        duration_ms=120_000,
        width=1280 if has_video else None,
        height=height if has_video else None,
        fps=30.0 if has_video else None,
        has_audio=True,
        has_video=has_video,
        video_codec="h264" if has_video else None,
        audio_codec="aac",
        audio_channels=2,
        container="mp4",
        bitrate=1_500_000,
        size_bytes=800_000,
        raw={"format": {}},
    )


class _State(dict):
    """Mutable knobs and counters shared with the fake extractor."""

    def __init__(self) -> None:
        super().__init__(
            fail_times=0,
            error="Connection reset by peer",
            download_calls=0,
            extract_calls=0,
            written_bytes=32 * 1024,
            progress=None,
        )


class _FakeYDL:
    def __init__(self, options: dict, state: _State) -> None:
        self.options = options
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = False):
        if not download:
            self.state["extract_calls"] += 1
            return INFO
        self.state["download_calls"] += 1
        if self.state["fail_times"]:
            self.state["fail_times"] -= 1
            raise RuntimeError(self.state["error"])
        hook = (self.options.get("progress_hooks") or [None])[0]
        if hook and self.state.get("progress"):
            hook(self.state["progress"])
        target = Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp4"))
        target.write_bytes(b"0" * self.state["written_bytes"])
        return {"requested_downloads": [{"filepath": str(target)}], "title": INFO["title"]}


class _FakeModule:
    def __init__(self, state: _State) -> None:
        self.state = state

    def YoutubeDL(self, options: dict) -> _FakeYDL:  # noqa: N802 - mirrors yt_dlp's API
        return _FakeYDL(options, self.state)


@pytest.fixture()
def storage(settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


@pytest.fixture()
def wired(monkeypatch, settings, project, storage):
    """A project wired to a fake extractor, probe and derived-asset builder."""
    state = _State()
    monkeypatch.setattr(importer, "_yt_dlp_module", lambda: _FakeModule(state))
    monkeypatch.setattr(importer, "probe", lambda path, **kwargs: _media_info())
    monkeypatch.setattr(importer, "sha256_file", lambda path: "hash-" + path.name)
    monkeypatch.setattr(importer, "RETRY_BASE_SECONDS", 0.0)

    def fake_proxy(source_path, destination, *, dimensions, duration_ms, cancel=None, on_progress=None):
        destination.write_bytes(b"p" * 4096)
        return {
            "width": dimensions[0],
            "height": dimensions[1],
            "crf": 30,
            "preset": "veryfast",
            "duration_ms": duration_ms,
            "audio": "dropped",
            "size_bytes": 4096,
        }

    monkeypatch.setattr(importer, "build_proxy", fake_proxy)

    def fake_extract(source_path, destination, *, cancel=None, duration_ms=0, on_progress=None):
        destination.write_bytes(b"w" * 8192)
        return {"duration_ms": duration_ms}

    monkeypatch.setattr(importer, "extract_audio", fake_extract)
    return state


# ---------------------------------------------------------------------------
# Progress and error mapping
# ---------------------------------------------------------------------------


def test_progress_bands_are_ordered_and_bounded():
    # given
    stages = [
        "inspecting",
        "selecting_format",
        "downloading_video",
        "verifying",
        "preparing_audio",
        "preparing_proxy",
    ]

    # when
    starts = [progress_band(stage, 0) for stage in stages]
    ends = [progress_band(stage, 100) for stage in stages]

    # then
    assert starts == sorted(starts)
    assert ends == sorted(ends)
    assert starts[0] >= 0 and ends[-1] <= 100


def test_indeterminate_progress_sits_at_the_band_start():
    assert progress_band("inspecting", None) == progress_band("inspecting", 0)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("ERROR: Private video. Sign in if you've been granted access", "url_private"),
        ("This video is age-restricted", "url_age_restricted"),
        ("Video is DRM protected", "url_drm"),
        ("Members-only content", "url_members_only"),
        ("This video is not available in your country", "url_region_locked"),
        ("Unsupported URL: https://example.com/x", "url_unsupported"),
        ("Video unavailable", "url_unavailable"),
        ("Requested format is not available", "url_format_unavailable"),
    ],
)
def test_access_failures_get_specific_codes(message, code):
    # when
    mapped = _map_error(RuntimeError(message))

    # then
    assert mapped.code == code
    assert "link" not in mapped.message.lower() if code != "url_unsupported" else True


def test_transient_failures_are_retried_but_permanent_ones_are_not():
    assert importer._is_transient(RuntimeError("Connection reset by peer")) is True
    assert importer._is_transient(RuntimeError("HTTP Error 503: Service Unavailable")) is True
    assert importer._is_transient(RuntimeError("Private video")) is False
    # A message that matches a permanent marker wins over a transient one.
    assert importer._is_transient(RuntimeError("Private video: connection reset")) is False


# ---------------------------------------------------------------------------
# Download behaviour
# ---------------------------------------------------------------------------


def test_download_is_retried_then_succeeds(wired, repos, settings, project, storage):
    # given: the first two attempts drop the connection
    wired["fail_times"] = 2

    # when
    record, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then
    assert wired["download_calls"] == 3
    assert record["kind"] == "url"
    assert record["height"] == 720
    assert metrics["reused"] is False
    assert metrics["download"]["selector"] == "136+140"
    assert metrics["download"]["approximate"] is True


def test_permanent_failure_is_not_retried_and_leaves_no_partial(wired, repos, settings, project, storage):
    # given
    wired["fail_times"] = 99
    wired["error"] = "Private video. Sign in if you've been granted access"

    # when / then
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    assert raised.value.code == "url_private"
    assert wired["download_calls"] == 1
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("source/*")) == []
    assert repos.sources.find_optional(project["id"]) is None


def _interrupted_module(state: _State, *, error: str, leaves_partial: bool):
    """A fake extractor that dies mid-transfer, optionally leaving yt-dlp's fragment."""

    class _InterruptedYDL(_FakeYDL):
        def extract_info(self, url: str, download: bool = False):
            if not download:
                return INFO
            self.state["download_calls"] += 1
            if leaves_partial:
                target = Path(str(self.options["outtmpl"]).replace("%(ext)s", "f399.mp4.part"))
                target.write_bytes(b"x" * 4096)
            raise RuntimeError(error)

    class _Module:
        def YoutubeDL(self, options: dict) -> _InterruptedYDL:  # noqa: N802 - mirrors yt_dlp's API
            return _InterruptedYDL(options, state)

    return _Module()


def test_the_resume_key_is_stable_for_a_source_and_rendition(settings, project):
    # given
    paths = ensure_layout(settings, project["id"])

    # when
    first = importer._download_stem(paths, url=URL, selector="136+140")
    again = importer._download_stem(paths, url=URL, selector="136+140")
    other_rendition = importer._download_stem(paths, url=URL, selector="134+140")

    # then: the same source resumes the same file, a different rendition does not
    assert first == again
    assert first != other_rendition
    assert first.parent == paths / "source"


def test_a_transient_outage_keeps_the_fragment_for_the_retry(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: the connection drops and yt-dlp's fragment is already on disk
    monkeypatch.setattr(importer, "DOWNLOAD_RETRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _interrupted_module(wired, error="Connection reset by peer", leaves_partial=True),
    )

    # when
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    # then: the failure is marked retryable and the bytes were not thrown away
    assert raised.value.code == "url_download_failed"
    assert raised.value.retryable is True
    paths = ensure_layout(settings, project["id"])
    assert len(list(paths.glob("source/*.part"))) == 1
    assert repos.sources.find_optional(project["id"]) is None


def test_a_permanent_failure_discards_the_fragment(
    monkeypatch, wired, repos, settings, project, storage
):
    # given
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _interrupted_module(
            wired,
            error="Private video. Sign in if you've been granted access",
            leaves_partial=True,
        ),
    )

    # when / then: a source that can never be fetched is not worth keeping bytes for
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    assert raised.value.code == "url_private"
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("source/*")) == []


def test_a_retry_resumes_the_file_an_interrupted_run_started(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: one interrupted run that leaves a fragment behind
    monkeypatch.setattr(importer, "DOWNLOAD_RETRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _interrupted_module(wired, error="Connection reset by peer", leaves_partial=True),
    )
    with pytest.raises(JobFailed):
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())
    paths = ensure_layout(settings, project["id"])
    (fragment,) = paths.glob("source/*.part")

    # when: the user retries and the extractor reports the target it was given
    targets: list[str] = []

    class _RecordingYDL(_FakeYDL):
        def extract_info(self, url: str, download: bool = False):
            if download:
                targets.append(str(self.options["outtmpl"]))
            return super().extract_info(url, download=download)

    class _Module:
        def YoutubeDL(self, options: dict) -> _RecordingYDL:  # noqa: N802 - mirrors yt_dlp's API
            return _RecordingYDL(options, wired)

    monkeypatch.setattr(importer, "_yt_dlp_module", lambda: _Module())

    record, _ = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then: it continued the interrupted file instead of starting a new download
    assert record["kind"] == "url"
    assert Path(targets[-1].replace("%(ext)s", "f399.mp4.part")) == fragment
    # and the resume record was retired with the finished download
    assert list(paths.glob("source/*.resume.json")) == []


def _watching_module(state: _State, observed: list[bool], fragment: Path, *, error: str):
    """A fake extractor that reports whether a fragment was present when it ran."""

    class _WatchingYDL(_FakeYDL):
        def extract_info(self, url: str, download: bool = False):
            if download:
                observed.append(fragment.exists())
                raise RuntimeError(error)
            return INFO

    class _Module:
        def YoutubeDL(self, options: dict) -> _WatchingYDL:  # noqa: N802 - mirrors yt_dlp's API
            return _WatchingYDL(options, state)

    return _Module()


def test_a_fragment_from_another_rendition_is_not_resumed(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: bytes left by a download of a different rendition
    paths = ensure_layout(settings, project["id"])
    stem = importer._download_stem(paths, url=URL, selector="136+140")
    fragment = Path(f"{stem}.f399.mp4.part")
    fragment.write_bytes(b"x" * 4096)
    importer._write_manifest(stem, {
        "version": importer.RESUME_MANIFEST_VERSION,
        "url": URL,
        "selector": "134+140",
        "started_at": "2026-01-01T00:00:00Z",
    })
    observed: list[bool] = []
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _watching_module(wired, observed, fragment, error="Private video"),
    )

    # when
    with pytest.raises(JobFailed):
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    # then: the bytes were removed before the transfer, not appended to
    assert observed == [False]
    assert not fragment.exists()


def test_a_fragment_without_a_manifest_is_not_resumed(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: bytes whose source and rendition cannot be proven
    paths = ensure_layout(settings, project["id"])
    stem = importer._download_stem(paths, url=URL, selector="136+140")
    fragment = Path(f"{stem}.f399.mp4.part")
    fragment.write_bytes(b"x" * 4096)
    observed: list[bool] = []
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _watching_module(wired, observed, fragment, error="Private video"),
    )

    # when
    with pytest.raises(JobFailed):
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    # then
    assert observed == [False]


def test_a_download_shorter_than_declared_is_discarded(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: the transfer completes but the file is not the declared length
    short = _media_info()
    short.duration_ms = 30_000
    monkeypatch.setattr(importer, "probe", lambda path, **kwargs: short)

    # when
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    # then: nothing unverified is stored, and a retry starts clean
    assert raised.value.code == "url_download_failed"
    assert repos.sources.find_optional(project["id"]) is None
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("source/*")) == []


def test_transient_retries_stop_at_the_attempt_cap(
    monkeypatch, wired, repos, settings, project, storage
):
    # given: the connection never comes back
    monkeypatch.setattr(importer, "DOWNLOAD_RETRY_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(
        importer,
        "_yt_dlp_module",
        lambda: _interrupted_module(wired, error="Connection reset by peer", leaves_partial=False),
    )

    # when / then: a fast-failing endpoint cannot spin forever
    with pytest.raises(JobFailed):
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated())

    assert wired["download_calls"] == 3


def test_cancellation_stops_before_writing_and_cleans_up(wired, repos, settings, project, storage):
    # given: the extractor runs once, then the job is cancelled
    calls = {"count": 0}

    def should_cancel() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    # when / then
    with pytest.raises(JobFailed) as raised:
        download_remote(
            repos,
            settings,
            storage,
            project_id=project["id"],
            validated=_validated(),
            should_cancel=should_cancel,
        )

    assert raised.value.code == "cancelled"
    assert wired["download_calls"] == 0
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("source/*")) == []


def test_a_second_download_of_the_same_source_is_reused(wired, repos, settings, project, storage):
    # given
    first, _ = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # when
    second, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then
    assert wired["download_calls"] == 1
    assert second["id"] == first["id"]
    assert metrics["reused"] is True
    assert metrics["download"]["bytes_downloaded"] == 0


def test_a_different_format_is_downloaded_rather_than_reused(wired, repos, settings, project, storage):
    # given
    first, _ = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # when: the same URL, a different rendition
    second, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="lowest"
    )

    # then
    assert wired["download_calls"] == 2
    assert metrics["reused"] is False
    assert second["id"] == first["id"]


def test_largest_retained_source_is_never_replaced_by_a_proxy(wired, repos, settings, project, storage):
    # when
    record, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then: the retained source is the 720p download, and the proxy has its own row
    assert record["height"] == 720
    proxy = repos.media.find(record["id"], "proxy")
    assert proxy is not None
    # The proxy fits the long edge inside the configured cap. 1280x720 capped at
    # 480 becomes 480x270, never a crop and never a different aspect ratio.
    assert (proxy["width"], proxy["height"]) == (480, 270)
    assert proxy["has_audio"] is False
    assert proxy["source_identity"].startswith("hash-")
    assert metrics["proxy"]["height"] == 270
    # The proxy lives under its own path, never the source's.
    assert proxy["relative_path"].startswith(f"projects/{project['id']}/proxy/")
    assert record["relative_path"].startswith(f"projects/{project['id']}/source/")


def test_a_proxy_can_be_disabled_for_the_same_source(wired, repos, settings, project, storage):
    # when
    record, metrics = download_remote(
        repos,
        settings,
        storage,
        project_id=project["id"],
        validated=_validated(),
        processing="custom",
        quality="720p",
        proxy_height=0,
    )

    # then
    assert metrics["proxy"]["present"] is False
    assert repos.media.find(record["id"], "proxy") is None

def test_audio_is_extracted_into_its_own_asset(wired, repos, settings, project, storage):
    # when
    record, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then
    audio = repos.media.find(record["id"], "audio")
    assert audio is not None
    assert audio["relative_path"] == f"projects/{project['id']}/audio/audio.wav"
    assert audio["audio_codec"] == "pcm_s16le"
    assert audio["settings"]["sample_rate"] == 16_000
    assert audio["has_audio"] is True
    assert metrics["audio"]["present"] is True


def test_failed_proxy_generation_does_not_fail_the_import(wired, repos, settings, project, storage, monkeypatch):
    # given
    def broken(*args, **kwargs):
        raise RuntimeError("encoder crashed")

    monkeypatch.setattr(importer, "build_proxy", broken)

    # when
    record, metrics = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then
    assert record["id"]
    assert metrics["proxy"]["present"] is False
    assert repos.media.find(record["id"], "proxy") is None
    # A failed encode must not leave a half-written file behind.
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("proxy/*")) == []


def test_cancelling_the_proxy_stops_the_import(wired, repos, settings, project, storage, monkeypatch):
    # given: the job is cancelled while the proxy is encoding
    def cancelled(*args, **kwargs):
        raise ProcessCancelled()

    monkeypatch.setattr(importer, "build_proxy", cancelled)

    # when / then
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p")

    assert raised.value.code == "cancelled"
    assert repos.media.find(repos.sources.get(project["id"])["id"], "proxy") is None
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("proxy/*")) == []


def test_cancelling_audio_extraction_removes_the_partial(wired, repos, settings, project, storage, monkeypatch):
    # given
    def cancelled(source_path, destination, **kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"partial")
        raise ProcessCancelled()

    monkeypatch.setattr(importer, "extract_audio", cancelled)

    # when / then
    with pytest.raises(JobFailed) as raised:
        download_remote(repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p")

    assert raised.value.code == "cancelled"
    paths = ensure_layout(settings, project["id"])
    assert list(paths.glob("audio/*")) == []


def test_reuse_finishes_assets_a_previous_run_left_undone(
    wired, repos, settings, project, storage, monkeypatch
):
    # given: the first import could not build a proxy
    def broken(*args, **kwargs):
        raise RuntimeError("encoder crashed")

    monkeypatch.setattr(importer, "build_proxy", broken)
    record, first = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )
    assert first["proxy"]["present"] is False

    # when: the extraction is made available and the same source is imported again
    def fake_proxy(source_path, destination, *, dimensions, duration_ms, cancel=None, on_progress=None):
        destination.write_bytes(b"p" * 4096)
        return {
            "width": dimensions[0],
            "height": dimensions[1],
            "crf": 30,
            "preset": "veryfast",
            "duration_ms": duration_ms,
            "audio": "dropped",
            "size_bytes": 4096,
        }

    monkeypatch.setattr(importer, "build_proxy", fake_proxy)
    second_record, second = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )

    # then: nothing was downloaded, but the missing proxy now exists
    assert wired["download_calls"] == 1
    assert second["reused"] is True
    assert second_record["id"] == record["id"]
    assert second["proxy"]["present"] is True
    assert repos.media.find(record["id"], "proxy") is not None


def test_reuse_does_not_re_extract_audio(wired, repos, settings, project, storage):
    # given
    record, _ = download_remote(
        repos, settings, storage, project_id=project["id"], validated=_validated(), quality="720p"
    )
    source_path = storage.path_for(record["relative_path"])

    # when
    audio = importer._make_audio_asset(
        repos,
        storage,
        project_id=project["id"],
        source=repos.sources.get(project["id"]),
        source_path=source_path,
        duration_ms=120_000,
        cancel_event=None,
        report=None,
    )

    # then: the existing asset is returned untouched
    assert audio is not None
    assert audio["relative_path"] == f"projects/{project['id']}/audio/audio.wav"


def test_download_summary_reports_speed_and_marks_estimates(wired, repos, settings, project, storage):
    # given
    choice = choose_format(INFO, "720p")
    plan = importer.resolve_plan(mode="balanced", quality="720p", source_height=choice.height)

    # when
    summary = _download_summary(choice, plan, size_bytes=800_000, elapsed_seconds=4.0, bytes_downloaded=800_000)

    # then
    assert summary["speed_bps"] == 200_000
    assert summary["size_bytes"] == 800_000
    assert summary["estimated_bytes"] == choice.estimated_bytes
    assert summary["approximate"] is True


# ---------------------------------------------------------------------------
# Cleanup and reuse decisions
# ---------------------------------------------------------------------------


def test_cleanup_removes_partials_but_not_a_stored_source(settings, project):
    # given
    paths = ensure_layout(settings, project["id"])
    (paths / "source" / "abc.mp4.part").write_bytes(b"x")
    (paths / "source" / "abc.mp4.ytdl").write_text("{}")
    keep = paths / "source" / "abc.mp4"
    keep.write_bytes(b"x")

    # when
    cleanup_partials(settings, project["id"])

    # then
    assert keep.exists()
    assert not (paths / "source" / "abc.mp4.part").exists()
    assert not (paths / "source" / "abc.mp4.ytdl").exists()


def test_reuse_requires_the_same_url_selector_and_present_file(repos, settings, project, storage):
    # given
    paths = ensure_layout(settings, project["id"])
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    stored = storage.path_for(key)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"x")
    repos.sources.upsert(
        project_id=project["id"],
        kind="url",
        original_name="A talk",
        storage_path=str(stored),
        relative_path=key,
        size_bytes=1,
        duration_ms=120_000,
        width=1280,
        height=720,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
        source_url=URL,
        probe={},
        has_video=True,
        content_hash="hash",
        download={"selector": "136+140"},
    )
    choice = choose_format(INFO, "720p")

    # when
    reusable = _existing_reusable_source(repos, storage, project["id"], url=URL, choice=choice)
    other_selector = _existing_reusable_source(
        repos,
        storage,
        project["id"],
        url=URL,
        choice=choose_format(INFO, "lowest"),
    )
    other_url = _existing_reusable_source(
        repos, storage, project["id"], url=URL + "&x=1", choice=choice
    )

    # then
    assert reusable is not None
    assert other_selector is None
    assert other_url is None

    # when: the stored bytes disappear
    stored.unlink()
    missing = _existing_reusable_source(repos, storage, project["id"], url=URL, choice=choice)

    # then
    assert missing is None
    assert paths.exists()


def test_download_options_are_bounded_and_never_shell_out(settings):
    # when
    options = importer._base_options(settings)

    # then: one video only, bounded waits, and a cache inside our own temp dir
    assert options["noplaylist"] is True
    assert options["playlist_items"] == "1"
    assert options["socket_timeout"] > 0
    assert 0 <= options["retries"] <= 5
    assert options["fragment_retries"] <= 5
    assert str(settings.tmp_dir) in options["cachedir"]
    # A stalled CDN connection is the real-world bottleneck, so the transfer is
    # chunked and a handful of fragment sockets are allowed — never unbounded.
    assert options["http_chunk_size"] > 0
    assert 1 <= options["concurrent_fragment_downloads"] <= 8
    # yt-dlp receives a dict, not a command line, and nothing here lets a user
    # inject an external program or extra arguments.
    for forbidden in ("exec", "external_downloader", "external_downloader_args", "postprocessor_args"):
        assert forbidden not in options


def test_a_stalled_connection_is_reported_as_slow_not_as_progress(wired, repos, settings, project, storage):
    # given: a download that is technically alive but running at 5 KB/s
    wired["progress"] = {
        "status": "downloading",
        "downloaded_bytes": 668_000,
        "total_bytes": 442_000_000,
        "speed": 5_400,
        "eta": 99_653,
        "elapsed": 120,
        "info_dict": {"format_id": "136"},
    }
    reports: list[tuple[str, float | None, str, dict]] = []

    # when
    download_remote(
        repos,
        settings,
        storage,
        project_id=project["id"],
        validated=_validated(),
        quality="720p",
        on_progress=lambda *args: reports.append(args),
    )

    # then: the stage reports its own fraction, and says the link is too slow
    stage, percent, message, detail = next(item for item in reports if item[0] == "downloading_video")
    assert stage == "downloading_video"
    assert 0 < percent < 1
    assert detail["slow"] is True
    assert "very slow" in message
    assert detail["eta_seconds"] == 99_653


@pytest.mark.parametrize(
    ("info", "code"),
    [
        ({}, "url_unsupported"),
        ({"_type": "playlist", "duration": 10}, "url_playlist"),
        ({"is_live": True, "duration": 10}, "url_live"),
        ({"duration": None}, "invalid_duration"),
        ({"duration": 0}, "invalid_duration"),
    ],
)
def test_unplannable_sources_are_rejected_with_a_specific_code(settings, info, code):
    # when / then
    with pytest.raises(JobFailed) as raised:
        importer._validate_info(settings, info)
    assert raised.value.code == code


def test_a_source_longer_than_the_limit_is_rejected(settings, project, repos, storage):
    # when / then
    with pytest.raises(Exception) as raised:
        importer._validate_duration(settings, settings.max_duration_ms + 1)
    assert getattr(raised.value, "code", None) == "duration_too_long"
    assert "minute limit" in str(raised.value)


# ---------------------------------------------------------------------------
# Persisting a source
# ---------------------------------------------------------------------------


def test_persist_records_hash_and_video_presence(repos, settings, project, storage, monkeypatch):
    # given
    monkeypatch.setattr(importer, "probe", lambda path, **kwargs: _media_info())
    monkeypatch.setattr(importer, "sha256_file", lambda path: "stable-hash")
    paths = ensure_layout(settings, project["id"])
    temporary = paths / "source" / "download.mp4"
    temporary.write_bytes(b"v" * 20_000)

    # when
    record = persist_source(
        repos,
        settings,
        storage,
        project_id=project["id"],
        temporary_file=temporary,
        kind="url",
        original_name="A talk",
        source_url=URL,
    )

    # then
    assert record["content_hash"] == "stable-hash"
    assert record["has_video"] == 1
    assert json.loads(record["download_json"]) == {}
    assert storage.path_for(record["relative_path"]).exists()


def test_persist_accepts_an_audio_only_source(repos, settings, project, storage, monkeypatch):
    # given
    monkeypatch.setattr(importer, "probe", lambda path, **kwargs: _media_info(has_video=False))
    monkeypatch.setattr(importer, "sha256_file", lambda path: "audio-hash")
    paths = ensure_layout(settings, project["id"])
    temporary = paths / "source" / "audio.m4a"
    temporary.write_bytes(b"a" * 20_000)

    # when
    record = persist_source(
        repos,
        settings,
        storage,
        project_id=project["id"],
        temporary_file=temporary,
        kind="url",
        original_name="Audio only",
        source_url=URL,
        allow_audio_only=True,
    )

    # then
    assert record["has_video"] == 0
    assert record["has_audio"] == 1
    assert record["duration_ms"] == 120_000
    assert record["width"] is None
