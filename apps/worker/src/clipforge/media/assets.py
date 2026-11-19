"""Working copies derived from a retained source.

Analysis and rendering both prefer the cheapest file that can still answer their
question: the 16 kHz audio for speech and the low-resolution proxy for pictures.
Neither is ever an export source — the retained source is — so resolving them
here keeps that distinction in one place.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clipforge.db.repositories import Repositories
from clipforge.media.visual import DEFAULT_ANALYSIS_HEIGHT, LOW_EVIDENCE_HEIGHT
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local

logger = logging.getLogger(__name__)


def visual_input(
    repos: Repositories,
    storage: StorageBackend,
    source: dict,
    source_path: Path,
) -> tuple[Path, int]:
    """Where picture analysis should read from, and the height it will sample.

    A proxy is used when it exists and is readable; otherwise the retained source
    is sampled at a bounded height. The returned height is the evidence level, so
    callers can record what the conclusions were actually based on.
    """
    source_height = int(source.get("height") or DEFAULT_ANALYSIS_HEIGHT)
    proxy = repos.media.find(source["id"], "proxy")
    if proxy:
        try:
            proxy_path = ensure_local(
                storage,
                proxy.get("relative_path"),
                legacy_path=proxy.get("storage_path"),
                missing_message="The analysis proxy is missing from this project's workspace.",
            )
            return proxy_path, int(proxy.get("height") or source_height)
        except StorageObjectMissing:
            logger.info("Analysis proxy is missing; sampling the retained source instead")
    return source_path, min(source_height, LOW_EVIDENCE_HEIGHT)


def audio_input(repos: Repositories, storage: StorageBackend, source: dict) -> Path | None:
    """The extracted speech audio, when one has already been prepared."""
    asset = repos.media.find(source["id"], "audio")
    if not asset:
        return None
    try:
        return ensure_local(
            storage,
            asset.get("relative_path"),
            legacy_path=asset.get("storage_path"),
            missing_message="The extracted audio is missing from this project's workspace.",
        )
    except StorageObjectMissing:
        logger.info("Extracted audio is missing; it will be rebuilt from the source")
        return None


def proxy_input(repos: Repositories, storage: StorageBackend, source: dict) -> Path | None:
    """The analysis proxy alone, or ``None`` when this project has none."""
    proxy = repos.media.find(source["id"], "proxy")
    if not proxy:
        return None
    try:
        return ensure_local(
            storage,
            proxy.get("relative_path"),
            legacy_path=proxy.get("storage_path"),
            missing_message="The analysis proxy is missing from this project's workspace.",
        )
    except StorageObjectMissing:
        return None
