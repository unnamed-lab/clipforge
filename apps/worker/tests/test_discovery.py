"""Additional discovery: reuse, persistence, dedupe and failure behaviour.

Every test seeds the rows a completed analysis would have written, then runs the
real discovery service against a temporary database and workspace. No speech
model, network call or ffmpeg run is involved.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Settings
from clipforge.discovery.criteria import DiscoveryCriteria
from clipforge.discovery.coverage import build_coverage, coverage_summary
from clipforge.discovery.dedupe import duplicate_reason, meaningfully_different
from clipforge.discovery.service import run_discovery
from clipforge.errors import JobFailed
from clipforge.storage.local import LocalStorage

SENTENCES = [
    "Do you know why most founders hire too early?",
    "The biggest mistake founders make is hiring for the role they think they need.",
    "You should hire for the problem you actually have right now.",
    "We spent six months looking for a head of sales before we had a single customer.",
    "That was a very expensive way to learn a simple lesson.",
    "What changed everything was talking to twenty customers myself.",
    "The pattern became obvious within about two weeks.",
    "Then you can hire someone to do the work you have already proven.",
    "It sounds slower but it saves you a year.",
    "That is the whole idea behind doing things that do not scale.",
    "A surprising thing happened when we stopped guessing.",
    "It turns out the best advice was the simplest one.",
]


def _segments(sentences: list[str], speakers: list[str] | None = None) -> list[dict]:
    segments: list[dict] = []
    cursor = 0
    for index, sentence in enumerate(sentences):
        tokens = sentence.split()
        span = max(2_000, len(tokens) * 260)
        step = span / max(1, len(tokens))
        segment = {
            "id": index,
            "start_ms": cursor,
            "end_ms": cursor + span,
            "text": f" {sentence}",
            "words": [
                {
                    "word": f" {token}",
                    "start_ms": cursor + int(position * step),
                    "end_ms": cursor + int((position + 1) * step) - 30,
                    "probability": 0.9,
                }
                for position, token in enumerate(tokens)
            ],
        }
        if speakers:
            segment["speaker"] = speakers[index]
        segments.append(segment)
        cursor += span + 400
    return segments


def _candidate(
    candidate_id: str,
    start_ms: int,
    end_ms: int,
    excerpt: str,
    *,
    moment_type: str = "best_advice",
    speaker: str | None = None,
) -> dict:
    return {
        "id": candidate_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "title": candidate_id,
        "hook": "",
        "reason": "A complete, self-contained idea.",
        "category": "moment",
        "excerpt": excerpt,
        "scores": {},
        "overall": 74.0,
        "warnings": [],
        "method": "baseline",
        "method_version": "1",
        "boundary": {"suspicious": False, "evidence": [], "alternatives": []},
        "editorial": {},
        "duration": {},
        "moment_type": moment_type,
        "speaker": speaker,
        "brief": {"matched_types": [moment_type]},
    }


def _seed(
    repos,
    storage: LocalStorage,
    project: dict,
    *,
    existing: list[dict] | None = None,
    sentences: list[str] | None = None,
    speakers: list[str] | None = None,
) -> dict:
    sentences = sentences or SENTENCES
    existing = existing or [
        _candidate(
            "candidate-1",
            30_000,
            42_000,
            "What changed everything was talking to twenty customers myself. "
            "The pattern became obvious within about two weeks.",
        )
    ]
    segments = _segments(sentences, speakers)
    duration_ms = segments[-1]["end_ms"] + 1_000
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 2048)
    source = repos.sources.upsert(
        project_id=project["id"],
        kind="local",
        original_name="Source.mp4",
        storage_path=str(path),
        relative_path=key,
        size_bytes=2048,
        duration_ms=duration_ms,
        width=None,
        height=None,
        fps=None,
        has_audio=True,
        video_codec=None,
        audio_codec="aac",
        container="mp4",
        source_url=None,
        probe={},
        has_video=False,
    )
    repos.transcripts.save(
        project_id=project["id"],
        source_id=source["id"],
        fingerprint="fp",
        language="en",
        language_probability=0.99,
        model="tiny",
        duration_ms=duration_ms,
        segments=segments,
    )
    repos.briefs.save(
        project["id"],
        {"duration_preset": "custom", "min_duration_ms": 6_000, "max_duration_ms": 15_000},
    )
    run = repos.runs.create(
        project["id"],
        {
            "goal": "best_moments",
            "brief": {"duration_preset": "custom", "min_duration_ms": 6_000, "max_duration_ms": 15_000},
        },
        kind="initial",
        discovery={
            "kind": "initial",
            "requested": len(existing),
            "found": len(existing),
        },
    )
    repos.candidates.replace_for_run(project["id"], run["id"], existing)
    return source


def _run(repos, settings: Settings, storage: LocalStorage, project_id: str, criteria: dict, *, cancel=False):
    return run_discovery(
        repos,
        settings,
        storage,
        project_id=project_id,
        request={"criteria": criteria},
        on_stage=lambda *_args: None,
        should_cancel=lambda: cancel,
        cancel_event=threading.Event(),
    )


@pytest.fixture()
def storage(settings: Settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


def test_additional_discovery_persists_a_new_batch_and_keeps_existing(repos, settings, storage, project):
    source = _seed(repos, storage, project)
    before = {candidate["id"] for candidate in repos.candidates.list_for_project(project["id"])}

    result = _run(repos, settings, storage, project["id"], {"count": 4})

    after = repos.candidates.list_for_project(project["id"])
    assert result["found"] >= 1
    assert before.issubset({candidate["id"] for candidate in after})
    assert len(after) == len(before) + result["found"]
    assert result["total_candidates"] == len(after)

    run = repos.runs.latest(project["id"])
    assert run is not None
    assert run["kind"] == "additional"
    assert run["discovery"]["found"] == result["found"]
    assert run["discovery"]["requested"] == 4
    assert run["discovery"]["coverage"]["duration_ms"] == source["duration_ms"]
    assert result["coverage"]["ratio"] > 0


def test_increasing_the_count_does_not_regenerate_earlier_moments(repos, settings, storage, project):
    _seed(repos, storage, project)
    initial_total = len(repos.candidates.list_for_project(project["id"]))
    first = _run(repos, settings, storage, project["id"], {"count": 2})
    first_run = repos.runs.latest(project["id"])
    first_ids = {
        candidate["id"]
        for candidate in repos.candidates.list_for_project(project["id"], first_run["id"])
    }
    assert first["found"] == len(first_ids)

    second = _run(repos, settings, storage, project["id"], {"count": 4})
    second_run = repos.runs.latest(project["id"])
    assert second_run["id"] != first_run["id"]

    first_after = repos.candidates.list_for_project(project["id"], first_run["id"])
    assert {candidate["id"] for candidate in first_after} == first_ids
    assert len(first_after) == len(first_ids)

    everything = repos.candidates.list_for_project(project["id"])
    assert first_ids.issubset({candidate["id"] for candidate in everything})
    assert len(everything) == initial_total + first["found"] + second["found"]


def test_no_new_moments_reports_zero_and_preserves_existing(repos, settings, storage, project):
    existing = [
        _candidate(
            "candidate-1",
            0,
            15_000,
            " ".join(SENTENCES[:3]),
        )
    ]
    _seed(repos, storage, project, existing=existing)
    before = repos.candidates.list_for_project(project["id"])

    result = _run(
        repos,
        settings,
        storage,
        project["id"],
        {"count": 3, "source_start_ms": 0, "source_end_ms": 15_000},
    )

    assert result["found"] == 0
    assert result["reason"] == "duplicates_only"
    after = repos.candidates.list_for_project(project["id"])
    assert [candidate["id"] for candidate in after] == [candidate["id"] for candidate in before]
    run = repos.runs.latest(project["id"])
    assert run["kind"] == "additional"
    assert run["discovery"]["found"] == 0
    assert run["discovery"]["reason"] == "duplicates_only"


def test_targeted_type_only_returns_matching_moments(repos, settings, storage, project):
    _seed(
        repos,
        storage,
        project,
        existing=[
            _candidate(
                "candidate-1",
                40_000,
                52_000,
                "That is the whole idea behind doing things that do not scale.",
            )
        ],
    )

    result = _run(
        repos,
        settings,
        storage,
        project["id"],
        {"count": 3, "moment_types": ["question_answer"], "source_end_ms": 30_000},
    )

    assert result["found"] >= 1
    run = repos.runs.latest(project["id"])
    records = repos.candidates.list_for_project(project["id"], run["id"])
    assert records
    for record in records:
        assert "question_answer" in (record["brief"].get("matched_types") or [])


def test_source_range_limits_where_new_moments_come_from(repos, settings, storage, project):
    _seed(repos, storage, project)
    result = _run(
        repos,
        settings,
        storage,
        project["id"],
        {"count": 3, "source_start_ms": 18_000, "source_end_ms": 34_000},
    )
    assert result["found"] >= 1
    run = repos.runs.latest(project["id"])
    for record in repos.candidates.list_for_project(project["id"], run["id"]):
        assert record["start_ms"] >= 18_000
        assert record["end_ms"] <= 34_000


def test_speaker_targeting_without_labels_fails_clearly(repos, settings, storage, project):
    _seed(repos, storage, project)
    before = repos.candidates.list_for_project(project["id"])

    with pytest.raises(JobFailed) as caught:
        _run(
            repos,
            settings,
            storage,
            project["id"],
            {"count": 2, "include_speakers": ["SPEAKER_00"]},
        )

    assert caught.value.code == "speaker_labels_unavailable"
    after = repos.candidates.list_for_project(project["id"])
    assert [candidate["id"] for candidate in after] == [candidate["id"] for candidate in before]


def test_speaker_targeting_uses_recorded_labels(repos, settings, storage, project):
    speakers = ["SPEAKER_00"] * 6 + ["SPEAKER_01"] * 6
    _seed(
        repos,
        storage,
        project,
        speakers=speakers,
        existing=[
            _candidate(
                "candidate-1",
                40_000,
                52_000,
                "That is the whole idea behind doing things that do not scale.",
                speaker="SPEAKER_01",
            )
        ],
    )

    result = _run(
        repos,
        settings,
        storage,
        project["id"],
        {
            "count": 2,
            "include_speakers": ["SPEAKER_00"],
            "source_start_ms": 0,
            "source_end_ms": 20_000,
        },
    )

    assert result["found"] >= 1
    run = repos.runs.latest(project["id"])
    for record in repos.candidates.list_for_project(project["id"], run["id"]):
        assert record["speaker"] == "SPEAKER_00"


def test_cancellation_creates_no_batch(repos, settings, storage, project):
    _seed(repos, storage, project)
    runs_before = repos.runs.list_for_project(project["id"])

    with pytest.raises(JobFailed) as caught:
        _run(repos, settings, storage, project["id"], {"count": 2}, cancel=True)

    assert caught.value.code == "cancelled"
    assert len(repos.runs.list_for_project(project["id"])) == len(runs_before)


def test_missing_source_fails_without_losing_existing_moments(repos, settings, storage, project):
    source = _seed(repos, storage, project)
    before = repos.candidates.list_for_project(project["id"])
    storage.path_for(source["relative_path"]).unlink()

    with pytest.raises(JobFailed) as caught:
        _run(repos, settings, storage, project["id"], {"count": 2})

    assert caught.value.code == "source_missing"
    after = repos.candidates.list_for_project(project["id"])
    assert [candidate["id"] for candidate in after] == [candidate["id"] for candidate in before]


def test_existing_edits_survive_an_additional_search(repos, settings, storage, project):
    _seed(repos, storage, project)
    repos.candidates.update(
        project["id"], "candidate-1", start_ms=31_000, end_ms=41_000, decision=None, title=None
    )

    _run(repos, settings, storage, project["id"], {"count": 2})

    edited = repos.candidates.get(project["id"], "candidate-1")
    assert edited["user_start_ms"] == 31_000
    assert edited["user_end_ms"] == 41_000
    assert edited["start_ms"] == 31_000
    assert edited["edited"] is True


def test_new_candidates_carry_hooks_and_editorial_plans(repos, settings, storage, project):
    _seed(repos, storage, project)
    result = _run(repos, settings, storage, project["id"], {"count": 2})
    assert result["found"] >= 1
    run = repos.runs.latest(project["id"])
    records = repos.candidates.list_for_project(project["id"], run["id"])
    for record in records:
        assert isinstance(record["hook_detail"], dict)
        assert record["hook_detail"]
        assert record["editorial"]
        assert record["start_ms"] < record["end_ms"]


def test_coverage_reports_unexplored_sections():
    duration_ms = 600_000
    coverage = coverage_summary(
        build_coverage(duration_ms, [{"start_ms": 0, "end_ms": 60_000}])
    )
    assert coverage["duration_ms"] == duration_ms
    assert coverage["unexplored"] >= 1
    assert coverage["ratio"] < 0.5


def test_meaningfully_different_edit_is_kept_but_near_identical_is_not():
    model = SimpleNamespace(sentences=[])
    existing = {
        "id": "candidate-1",
        "start_ms": 0,
        "end_ms": 20_000,
        "excerpt": "we hired a head of sales too early and it cost us six months",
    }
    shifted = SimpleNamespace(
        start_ms=4_000,
        end_ms=30_000,
        duration_ms=26_000,
        excerpt="the real lesson was talking to twenty customers before hiring anyone at all",
    )
    same = SimpleNamespace(
        start_ms=0,
        end_ms=20_000,
        duration_ms=20_000,
        excerpt=existing["excerpt"],
    )

    assert meaningfully_different(shifted, existing, model) is True
    assert duplicate_reason(shifted, existing, model) is None
    assert meaningfully_different(same, existing, model) is False
    assert duplicate_reason(same, existing, model) == "existing:candidate-1"


def test_api_deduplicates_an_identical_running_request_and_reports_state(repos, settings, storage, project):
    _seed(repos, storage, project)
    criteria = DiscoveryCriteria(count=5)
    key = criteria.key()

    app = create_app(settings)
    with TestClient(app) as client:
        # Create the active job after startup recovery so it stays visible to the
        # idempotency check instead of being failed as an interrupted restart.
        active = repos.jobs.create(
            project_id=project["id"],
            job_type="discovery",
            payload={"project_id": project["id"], "criteria": criteria.model_dump(), "criteria_key": key},
            state="running",
        )
        client.headers.update({"x-clipforge-token": settings.token})
        response = client.post(
            f"/api/projects/{project['id']}/discovery",
            json={"count": 5},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["deduplicated"] is True
        assert payload["job"]["id"] == active["id"]
        assert payload["criteria_key"] == key

        state = client.get(f"/api/projects/{project['id']}/discovery")
        assert state.status_code == 200, state.text
        body = state.json()
        assert body["counts"]["total_discovered"] == 1
        assert body["counts"]["batches"] == 1
        assert body["batches"][0]["kind"] == "initial"
        assert body["coverage"]["duration_ms"] > 0
        assert body["generating"] is True


def test_api_rejects_discovery_before_a_first_analysis(repos, settings, storage, project):
    segments = _segments(SENTENCES)
    duration_ms = segments[-1]["end_ms"] + 1_000
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 2048)
    source = repos.sources.upsert(
        project_id=project["id"],
        kind="local",
        original_name="Fresh.mp4",
        storage_path=str(path),
        relative_path=key,
        size_bytes=2048,
        duration_ms=duration_ms,
        width=None,
        height=None,
        fps=None,
        has_audio=True,
        video_codec=None,
        audio_codec="aac",
        container="mp4",
        source_url=None,
        probe={},
        has_video=False,
    )
    repos.transcripts.save(
        project_id=project["id"],
        source_id=source["id"],
        fingerprint="fp",
        language="en",
        language_probability=0.99,
        model="tiny",
        duration_ms=duration_ms,
        segments=segments,
    )

    app = create_app(settings)
    with TestClient(app) as client:
        client.headers.update({"x-clipforge-token": settings.token})
        response = client.post(f"/api/projects/{project['id']}/discovery", json={"count": 5})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "analysis_required"
