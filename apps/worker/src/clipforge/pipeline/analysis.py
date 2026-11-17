"""The analysis pipeline: inspect -> audio -> transcript -> candidates -> ranking."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Callable

from clipforge.config import Settings
from clipforge.db.repositories import Repositories, new_id
from clipforge.errors import JobFailed
from clipforge.media.audio import extract_audio
from clipforge.media.waveform import render_waveform
from clipforge.pipeline.briefing import (
    apply_brief,
    apply_corrections,
    attach_editorial,
    attach_hooks,
    persist_brief,
    resolve_brief,
    summarise,
)
from clipforge.providers.llm.openai_compat import build_llm_provider
from clipforge.providers.llm.base import ScorableClip
from clipforge.providers.transcription.base import TranscriptionOptions
from clipforge.providers.transcription.faster_whisper import SUPPORTED_MODELS, FasterWhisperTranscription
from clipforge.quality.boundaries import review_ending
from clipforge.ranking.dedupe import select_candidates
from clipforge.ranking.generate import DURATION_BANDS, generate_candidates
from clipforge.ranking.score import score_candidates, weights_for
from clipforge.ranking.sentences import build_transcript_model
from clipforge.storage.base import StorageBackend, StorageObjectMissing, ensure_local
from clipforge.storage.workspace import ensure_layout, ensure_space, write_json_atomic

logger = logging.getLogger(__name__)

StageCallback = Callable[[str, float, str], None]
QUALITY_PROFILES = {
    "fast": {"beam_size": 1, "model": "base"},
    "balanced": {"beam_size": 5, "model": None},
    "best": {"beam_size": 5, "model": "medium"},
}
SHORTLIST_FOR_LLM = 26


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_fingerprint(source_hash: str, model: str, compute_type: str, language: str | None, vad: bool) -> str:
    payload = f"{source_hash}|{model}|{compute_type}|{language or 'auto'}|{int(vad)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def analysis_fingerprint(source_path: Path, options: TranscriptionOptions) -> str:
    return build_fingerprint(_sha256(source_path), options.model, options.compute_type, options.language, options.vad)


def _resolve_options(settings: Settings, request: dict) -> TranscriptionOptions:
    quality = str(request.get("quality") or "balanced")
    profile = QUALITY_PROFILES.get(quality, QUALITY_PROFILES["balanced"])
    model = str(request.get("model") or profile["model"] or settings.whisper_model)
    if model not in SUPPORTED_MODELS:
        model = settings.whisper_model if settings.whisper_model in SUPPORTED_MODELS else "small"
    language = request.get("language")
    return TranscriptionOptions(
        model=model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        language=(language or None),
        vad=bool(request.get("vad", True)),
        beam_size=int(profile["beam_size"]),
        model_dir=str(settings.models_dir),
    )


def run_analysis(
    repos: Repositories,
    settings: Settings,
    storage: StorageBackend,
    *,
    project_id: str,
    request: dict,
    on_stage: StageCallback,
    should_cancel: Callable[[], bool],
    cancel_event: threading.Event,
) -> dict:
    project = repos.projects.get(project_id)
    source = repos.sources.get(project_id)
    if not source["has_audio"]:
        raise JobFailed(
            "no_audio_stream",
            "This video has no audio track, so there is no speech to analyse.",
        )

    try:
        source_path = ensure_local(
            storage,
            source.get("relative_path"),
            legacy_path=source.get("storage_path"),
            missing_message="The source video is missing from this project's workspace.",
        )
    except StorageObjectMissing as exc:
        raise JobFailed("source_missing", exc.message) from exc

    brief, brief_source = resolve_brief(repos, project, request)
    persist_brief(repos, project, brief, brief_source)
    warnings: list[str] = []
    if brief_source == "legacy":
        warnings.append(
            "This project has no saved clipping brief, so a brief was inferred from the legacy goal settings."
        )

    options = _resolve_options(settings, request)
    on_stage("prepare", 1, "Reading source")
    ensure_space(settings, settings.projects_dir, required_bytes=600 * 1024 * 1024)
    paths = ensure_layout(settings, project_id)
    paths.mkdir(parents=True, exist_ok=True)

    goal = str(request.get("goal") or "best_moments")
    clip_count = brief.clip_count
    band_choice = str(request.get("duration_band") or "auto")

    provider = FasterWhisperTranscription()
    available, detail = provider.available()
    if not available:
        raise JobFailed("transcription_unavailable", detail or "Transcription is not available.")

    on_stage("prepare", 3, "Checking for an existing transcript")
    fingerprint = analysis_fingerprint(source_path, options)
    transcript = repos.transcripts.find_by_fingerprint(project_id, fingerprint)

    if transcript is not None:
        on_stage("transcribe", 60, "Reusing the existing transcript")
        duration_ms = int(transcript["duration_ms"])
        segments = transcript["segments"]
        language = transcript["language"]
        language_probability = transcript["language_probability"]
    else:
        if should_cancel():
            raise JobFailed("cancelled", "Cancelled")
        on_stage("audio", 5, "Extracting audio")
        audio_path = paths / "audio" / "audio.wav"
        extract_audio(
            source_path,
            audio_path,
            cancel=cancel_event,
            duration_ms=int(source["duration_ms"]),
            on_progress=lambda percent: on_stage("audio", 5 + percent * 0.05, "Extracting audio"),
        )
        if should_cancel():
            raise JobFailed("cancelled", "Cancelled")

        on_stage("transcribe", 12, f"Transcribing with {options.model} (CPU)")
        result = provider.transcribe(
            audio_path,
            options,
            on_progress=lambda percent: on_stage(
                "transcribe", 12 + percent * 0.50, f"Transcribing with {options.model} (CPU)"
            ),
            should_cancel=should_cancel,
        )
        duration_ms = result.duration_ms or int(source["duration_ms"])
        segments = [segment.to_dict() for segment in result.segments]
        language = result.language
        language_probability = result.language_probability
        transcript = repos.transcripts.save(
            project_id=project_id,
            source_id=source["id"],
            fingerprint=fingerprint,
            language=language,
            language_probability=language_probability,
            model=options.model,
            duration_ms=duration_ms,
            segments=segments,
        )
        write_json_atomic(paths / "analysis" / "transcript.json", {"segments": segments,
                                                                  "language": language,
                                                                  "model": options.model})

    if should_cancel():
        raise JobFailed("cancelled", "Cancelled")

    corrections = repos.corrections.list(project_id)
    if corrections:
        segments = apply_corrections(segments, corrections)
        warnings.append(f"Applied {len(corrections)} manual transcript correction(s).")

    render_waveform(source_path, paths / "analysis" / "waveform.png", cancel=cancel_event)

    on_stage("candidates", 63, "Finding candidate moments")
    model = build_transcript_model(segments, duration_ms, language)
    if not model.sentences:
        raise JobFailed("no_speech_detected", "The transcript has no usable sentences to build clips from.")

    bands = brief.generation_bands()
    if band_choice in {"short", "medium", "long"}:
        explicit = tuple(band for band in DURATION_BANDS if band[2] == band_choice)
        if explicit:
            bands = explicit
    drafts = generate_candidates(model, bands=bands)
    if not drafts:
        raise JobFailed(
            "no_candidates",
            "No clip candidates could be built from this transcript. The video may be too short or too sparse.",
        )

    on_stage("rank", 72, "Scoring candidates against the brief")
    duration_range = brief.duration_range()
    ranked = score_candidates(
        drafts,
        model,
        goal=goal,
        weight_overrides=request.get("weights") or None,
        duration_range=duration_range,
    )
    ranked, blocked = apply_brief(ranked, brief)
    if not ranked:
        raise JobFailed(
            "all_candidates_excluded",
            "Every candidate was excluded by the brief's exclusions or safety rules. "
            "Relax the brief or check the transcript.",
        )

    run_id = new_id()
    run = repos.runs.create(project_id, {"goal": goal, "clip_count": clip_count, "quality": request.get("quality"),
                                         "duration_band": band_choice, "model": options.model,
                                         "brief": brief.model_dump(), "brief_source": brief_source})
    run_id = run["id"]

    llm_provider = build_llm_provider(settings) if request.get("use_llm") else None
    if llm_provider is not None:
        available_llm, detail = llm_provider.available()
        if not available_llm:
            warnings.append(f"LLM rescoring was requested but is not configured ({detail}). Baseline scores used.")
            llm_provider = None
        elif _contains_private_transcript(request):
            warnings.append("LLM rescoring skipped: consent to send transcript text was not given.")
            llm_provider = None

    if llm_provider is not None:
        on_stage("rank", 82, "Refining the shortlist with the configured model")
        shortlist = ranked[:SHORTLIST_FOR_LLM]
        scorable = [
            ScorableClip(
                id=candidate.draft.key,
                start_ms=candidate.start_ms,
                end_ms=candidate.end_ms,
                text=candidate.excerpt,
                baseline_overall=candidate.overall,
            )
            for candidate in shortlist
        ]
        try:
            llm_result = llm_provider.score(scorable, goal=goal, language=language)
        except Exception as exc:  # provider failures must never break analysis
            logger.warning("LLM rescoring failed: %s", exc)
            warnings.append("The language model could not be reached, so deterministic scores were used.")
        else:
            by_id = {candidate.draft.key: candidate for candidate in shortlist}
            for scored in llm_result.clips:
                candidate = by_id.get(scored.id)
                if candidate is None:
                    continue
                candidate.overall = round(0.55 * scored.overall + 0.45 * candidate.overall, 1)
                candidate.method = "baseline+llm"
                candidate.method_version = f"baseline-1+{llm_result.model}"
                if scored.title:
                    candidate.title = scored.title[:120]
                if scored.hook:
                    candidate.hook = scored.hook[:240]
                if scored.reason:
                    candidate.reason = scored.reason[:600]
                for warning in scored.warnings:
                    if warning not in candidate.features.warnings:
                        candidate.features.warnings.append(warning)
            ranked.sort(key=lambda item: item.overall, reverse=True)

    if should_cancel():
        raise JobFailed("cancelled", "Cancelled")

    on_stage("rank", 90, "Removing near-duplicates")
    eligible = [candidate for candidate in ranked if candidate.overall >= brief.min_quality]
    if not eligible:
        eligible = ranked[: min(3, len(ranked))]
        warnings.append(
            f"No moment reached the brief's minimum quality of {brief.min_quality:.0f}/100. "
            "The closest matches are shown; review them before exporting."
        )
    elif len(eligible) < len(ranked):
        warnings.append(
            f"{len(ranked) - len(eligible)} candidate(s) scored below the brief's minimum quality threshold."
        )

    selection = select_candidates(eligible, limit=clip_count, duration_ms=duration_ms)
    if not selection.kept:
        raise JobFailed("no_candidates", "No clip candidates survived the quality checks.")

    if blocked:
        warnings.append(f"{blocked} candidate(s) were excluded by the brief's exclusions or safety rules.")
    warnings.extend(attach_hooks(selection.kept, model, brief))
    warnings.extend(attach_editorial(selection.kept, model, brief))

    fitting = [candidate for candidate in selection.kept if (candidate.duration or {}).get("status") == "fit"]
    if not fitting:
        closest = sorted(
            selection.kept,
            key=lambda candidate: abs(candidate.duration_ms - (
                duration_range.target_ms or (duration_range.min_ms + duration_range.max_ms) // 2
            )),
        )[:3]
        alternatives = "; ".join(
            f"{candidate.title} ({candidate.duration_ms / 1000:.0f}s, "
            f"{(candidate.duration or {}).get('status', 'unknown')})"
            for candidate in closest
        )
        warnings.append(
            f"No complete moment fits the {duration_range.label} target. "
            f"Closest coherent alternatives: {alternatives}. Nothing was padded or truncated."
        )

    on_stage("boundaries", 94, "Reviewing clip endings")
    now_payload = []
    for candidate in selection.kept:
        hook_detail = candidate.hook_detail or {}
        try:
            boundary = review_ending(
                candidate_start_ms=candidate.start_ms,
                candidate_end_ms=candidate.end_ms,
                sentences=model.sentences,
                source_path=source_path,
                source_duration_ms=int(source["duration_ms"]),
                cache_dir=paths / "analysis" / "scenes",
            )
        except Exception as exc:  # ending review must never break analysis
            logger.warning("Ending review failed for %s: %s", candidate.draft.key, exc)
            boundary = {
                "version": 1,
                "suspicious": False,
                "reviewed": "pending",
                "evidence": ["Ending review could not run."],
                "alternatives": [],
            }
        now_payload.append(
            {
                "id": new_id(),
                "start_ms": candidate.start_ms,
                "end_ms": candidate.end_ms,
                "title": candidate.title,
                "hook": candidate.hook,
                "hook_detail": hook_detail,
                "summary": candidate.summary or summarise(candidate.excerpt),
                "reason": candidate.reason,
                "category": candidate.category,
                "excerpt": candidate.excerpt,
                "scores": candidate.scores_payload(),
                "brief": candidate.brief,
                "overall": candidate.overall,
                "warnings": candidate.warnings,
                "method": candidate.method,
                "method_version": candidate.method_version,
                "boundary": boundary,
                "editorial": candidate.editorial,
                "duration": candidate.duration,
            }
        )

    suspicious = [candidate for candidate in now_payload if (candidate.get("boundary") or {}).get("suspicious")]
    if suspicious:
        warnings.append(
            f"{len(suspicious)} ending(s) may include a trailing or unrelated shot. "
            "Check the ending review before exporting."
        )

    repos.candidates.replace_for_run(project_id, run_id, now_payload)
    repos.runs.finish(run_id, status="completed", provider="baseline" if llm_provider is None else "baseline+llm",
                      warnings=warnings)
    repos.projects.update(project_id, status="analysed")
    write_json_atomic(
        paths / "analysis" / "candidates.json",
        {"run_id": run_id, "goal": goal, "brief": brief.model_dump(), "brief_source": brief_source,
         "weights": weights_for(goal), "candidates": now_payload,
         "suppressed": len(selection.suppressed), "excluded_by_brief": blocked, "warnings": warnings},
    )

    on_stage("done", 100, "Analysis complete")
    return {
        "analysis_run_id": run_id,
        "candidates": len(now_payload),
        "suppressed_duplicates": len(selection.suppressed),
        "excluded_by_brief": blocked,
        "brief_source": brief_source,
        "language": language,
        "language_probability": language_probability,
        "model": options.model,
        "transcript_cached": transcript is not None,
        "warnings": warnings,
    }


def _contains_private_transcript(request: dict) -> bool:
    """LLM calls require explicit consent; ``use_llm`` alone is not enough."""
    return not bool(request.get("llm_consent", False))
