"""Shared test fixtures.

Unit tests never touch the network, a speech model or the real ``data/`` tree:
the settings fixture points the app at a temporary data directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.config import Settings
from clipforge.db.connection import Database
from clipforge.db.repositories import Repositories


def make_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        host="127.0.0.1",
        port=8788,
        token="test-token",
        max_upload_bytes=512 * 1024 * 1024,
        max_duration_ms=2 * 60 * 60 * 1000,
        min_free_bytes=0,
        whisper_model="tiny",
        whisper_device="cpu",
        whisper_compute_type="int8",
        llm_enabled=False,
        llm_base_url="",
        llm_model="",
        llm_api_key="",
        llm_timeout_seconds=30,
    )


@pytest.fixture()
def settings(tmp_path):
    resolved = make_settings(tmp_path / "data")
    resolved.ensure_dirs()
    return resolved


@pytest.fixture()
def repos(settings):
    database = Database(settings.db_path)
    database.initialise()
    return Repositories(database)


@pytest.fixture()
def project(repos):
    return repos.projects.create(name="Test project")


def make_segments(sentences: list[str], *, first_start_ms: int = 400, sentence_ms: int = 4_000) -> list[dict]:
    """Build whisper-shaped segments with evenly spaced word timings."""
    segments: list[dict] = []
    cursor = first_start_ms
    for index, sentence in enumerate(sentences):
        tokens = sentence.split()
        step = sentence_ms / max(1, len(tokens))
        words = [
            {
                "start_ms": cursor + int(position * step),
                "end_ms": cursor + int((position + 1) * step) - 40,
                "word": f" {token}",
                "probability": 0.9,
            }
            for position, token in enumerate(tokens)
        ]
        segments.append(
            {
                "id": index,
                "start_ms": cursor,
                "end_ms": cursor + sentence_ms,
                "text": f" {sentence}",
                "words": words,
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
            }
        )
        cursor += sentence_ms + 300
    return segments


@pytest.fixture()
def sample_sentences() -> list[str]:
    return [
        "The biggest mistake founders make is hiring for the role they think they need.",
        "You should hire for the problem you actually have right now.",
        "We spent six months looking for a head of sales before we had a single customer.",
        "That was a very expensive way to learn a simple lesson.",
        "Instead, talk to twenty customers yourself and write down what they say.",
        "The pattern becomes obvious within about two weeks.",
        "Then you can hire someone to do the work you have already proven.",
        "It sounds slower but it saves you a year.",
        "That is the whole idea behind doing things that do not scale.",
    ]


@pytest.fixture()
def sample_segments(sample_sentences) -> list[dict]:
    return make_segments(sample_sentences)
