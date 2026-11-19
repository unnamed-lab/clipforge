"""Candidate-level editorial endpoints: hook options and safe re-evaluation.

These run the real app against a temporary database. They do not run analysis:
the fixtures seed the transcript, run and candidate rows that analysis would
have produced, so the endpoints are exercised on their real inputs.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Settings
from clipforge.storage.local import LocalStorage

TRANSCRIPT = [
    "Do you know why most founders hire too early?",
    "The biggest mistake founders make is hiring for the role they think they need.",
    "We spent six months looking for a head of sales before our first customer.",
    "That was a very expensive way to learn a simple lesson.",
    "What changed everything was talking to twenty customers myself.",
    "The pattern became obvious within about two weeks.",
]


def _segments() -> list[dict]:
    """Whisper-shaped segments with real word timings, one sentence each."""
    segments: list[dict] = []
    cursor = 0
    for index, sentence in enumerate(TRANSCRIPT):
        tokens = sentence.split()
        span = max(2_000, len(tokens) * 260)
        step = span / max(1, len(tokens))
        segments.append(
            {
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
        )
        cursor += span + 200
    return segments


@pytest.fixture()
def storage(settings: Settings) -> LocalStorage:
    return LocalStorage(settings.data_dir)


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"x-clipforge-token": settings.token})
        yield test_client


@pytest.fixture()
def seeded(repos, storage, project):
    key = f"projects/{project['id']}/source/{uuid.uuid4().hex}.mp4"
    path = storage.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 2048)
    source = repos.sources.upsert(
        project_id=project["id"],
        kind="url",
        original_name="Source",
        storage_path=str(path),
        relative_path=key,
        size_bytes=2048,
        duration_ms=40_000,
        width=1920,
        height=1080,
        fps=30.0,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
        source_url="https://example.com/watch?v=abc",
        probe={},
        has_video=True,
    )
    repos.transcripts.save(
        project_id=project["id"],
        source_id=source["id"],
        fingerprint="fp",
        language="en",
        language_probability=0.99,
        model="tiny",
        duration_ms=40_000,
        segments=_segments(),
    )
    run = repos.runs.create(project["id"], {"brief": {"duration_preset": "extended"}})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [
            {
                "id": "candidate-1",
                "start_ms": 3_000,
                "end_ms": 18_000,
                "title": "Hiring mistake",
                "excerpt": (
                    "The biggest mistake founders make is hiring for the role they "
                    "think they need. We spent six months looking for a head of sales "
                    "before our first customer."
                ),
                "method": "baseline",
                "method_version": "1",
                "overall": 80.0,
            }
        ],
    )
    return project


def test_hooks_endpoint_lists_only_real_transcript_lines(client, seeded):
    # when
    response = client.get(f"/api/projects/{seeded['id']}/candidates/candidate-1/hooks")

    # then
    assert response.status_code == 200
    payload = response.json()
    assert payload["hooks"], "a clip that opens on a strong line must offer at least one hook"
    transcript_text = " ".join(TRANSCRIPT)
    for hook in payload["hooks"]:
        assert hook["text"] in transcript_text
        assert hook["end_ms"] > hook["start_ms"]
        assert hook["direction"] in {"opening", "before", "after"}
    assert payload["policy"]["enabled"] is True
    assert payload["reason"] is None


def test_hooks_endpoint_explains_itself_before_analysis(client, repos, project):
    # given a project with a candidate but no transcript
    run = repos.runs.create(project["id"], {})
    repos.candidates.replace_for_run(
        project["id"],
        run["id"],
        [
            {
                "id": "candidate-1",
                "start_ms": 0,
                "end_ms": 8_000,
                "title": "Moment",
                "method": "baseline",
                "method_version": "1",
            }
        ],
    )

    # when
    response = client.get(f"/api/projects/{project['id']}/candidates/candidate-1/hooks")

    # then
    assert response.status_code == 200
    payload = response.json()
    assert payload["hooks"] == []
    assert payload["reason"] == "no_transcript"


def test_editorial_re_evaluation_is_refused_while_analysis_runs(client, repos, seeded):
    # given an analysis job is still queued for this project
    repos.jobs.create(project_id=seeded["id"], job_type="analysis")

    # when
    response = client.post(
        f"/api/projects/{seeded['id']}/candidates/candidate-1/editorial",
        json={"strategy": "automatic"},
    )

    # then the mutation is refused instead of being overwritten by the run
    assert response.status_code == 409
    assert "Analysis is still running" in response.json()["error"]["message"]


def test_loop_strategy_is_accepted_and_recorded_as_a_deliberate_repeat(client, repos, seeded):
    # when
    response = client.post(
        f"/api/projects/{seeded['id']}/candidates/candidate-1/editorial",
        json={"strategy": "loop"},
    )

    # then
    assert response.status_code == 200
    editorial = response.json()["candidate"]["editorial"]
    timeline = editorial["timeline"]
    loop_entries = [entry for entry in timeline["entries"] if entry["role"] == "loop"]
    if editorial["strategy"] == "loop":
        assert loop_entries
        assert timeline["loop_ms"] > 0
        assert timeline["duplicated_ms"] == 0
        assert timeline["valid"] is True
    else:
        # The moment may legitimately be too short to replay; the fallback must say so.
        assert editorial["strategy"] == "chronological"
        assert editorial["fallback"] == "loop"


def test_hook_patch_accepts_an_offered_transcript_line(client, seeded):
    # given a hook option that the endpoint itself ranked from the transcript
    option = client.get(
        f"/api/projects/{seeded['id']}/candidates/candidate-1/hooks"
    ).json()["hooks"][0]

    # when
    response = client.patch(
        f"/api/projects/{seeded['id']}/candidates/candidate-1",
        json={
            "hook": {
                key: option[key]
                for key in ("text", "start_ms", "end_ms", "source", "placement", "direction")
            }
        },
    )

    # then
    assert response.status_code == 200
    saved = response.json()["candidate"]["hook_detail"]
    assert saved["text"] == option["text"]
    assert saved["start_ms"] == option["start_ms"]
    assert saved["approved"] is True


def test_hook_patch_refuses_invented_dialogue(client, seeded):
    # given a real span but wording the speaker never used
    option = client.get(
        f"/api/projects/{seeded['id']}/candidates/candidate-1/hooks"
    ).json()["hooks"][0]

    # when
    response = client.patch(
        f"/api/projects/{seeded['id']}/candidates/candidate-1",
        json={
            "hook": {
                "text": "This line was never spoken by anyone in the source.",
                "start_ms": option["start_ms"],
                "end_ms": option["end_ms"],
            }
        },
    )

    # then
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "hook_not_grounded"
    assert "verbatim" in response.json()["error"]["message"]
