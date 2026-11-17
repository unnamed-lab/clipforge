"""Glue between the stored brief, the ranking pass and the hook selector."""

from __future__ import annotations

from clipforge.briefs.match import blend_with_brief, evaluate_brief
from clipforge.briefs.spec import ClippingBrief, brief_from_legacy
from clipforge.db.repositories import Repositories
from clipforge.durations import duration_record
from clipforge.editorial.narrative import build_retention_plan
from clipforge.errors import NotFound, ValidationFailed
from clipforge.hooks.select import build_hook, build_hook_candidates
from clipforge.ranking.score import ScoredCandidate
from clipforge.ranking.sentences import TranscriptModel

BRIEF_SOURCES = ("request", "project", "profile", "legacy")


def project_profile(repos: Repositories, project: dict) -> dict | None:
    profile_id = project.get("profile_id")
    if not profile_id:
        return None
    try:
        return repos.profiles.get(profile_id)
    except NotFound:
        return None


def _merge(base: dict, override: dict) -> dict:
    """Merge a partial request brief onto the effective saved brief.

    A request that only changes the duration must not silently discard the
    profile's hook, editorial, safety or branding rules.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_brief(
    repos: Repositories,
    project: dict,
    request: dict,
) -> tuple[ClippingBrief, str]:
    """Pick the most specific valid brief and report where it came from."""
    profile = project_profile(repos, project)
    base_source = "legacy"
    base: dict = brief_from_legacy(
        str(request.get("goal") or "best_moments"),
        int(request.get("clip_count") or 8),
        str(request.get("duration_band") or "auto"),
        instructions=request.get("instructions"),
    ).model_dump()
    if profile and (profile.get("document") or {}).get("brief"):
        base = profile["document"]["brief"]
        base_source = "profile"
    if project.get("brief"):
        base = project["brief"]
        base_source = "project"

    if isinstance(request.get("brief"), dict):
        payload = _merge(base, request["brief"])
        source = "request"
    else:
        payload = base
        source = base_source

    try:
        return ClippingBrief.model_validate(payload), source
    except ValueError as exc:
        raise ValidationFailed(
            f"The clipping brief is not valid ({source}): {exc}",
            code="invalid_brief",
        ) from exc


def persist_brief(repos: Repositories, project: dict, brief: ClippingBrief, source: str) -> None:
    """Store the brief that a run actually used, once, without churning versions."""
    if source == "request" or not project.get("brief"):
        repos.briefs.save(project["id"], brief.model_dump())


def summarise(text: str, limit: int = 220) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def apply_brief(ranked: list[ScoredCandidate], brief: ClippingBrief) -> tuple[list[ScoredCandidate], int]:
    """Blend explainable brief relevance into every score; drop excluded moments."""
    blocked = 0
    for candidate in ranked:
        components = {key: value.score for key, value in candidate.features.components.items()}
        match = evaluate_brief(
            brief,
            text=candidate.excerpt,
            category=candidate.category,
            components=components,
        )
        candidate.brief = match.to_dict()
        candidate.brief_relevance = match.relevance
        if match.blocked:
            blocked += 1
            candidate.overall = 0.0
            if match.blocked_reason:
                candidate.features.warnings.append(match.blocked_reason)
            continue
        candidate.overall = blend_with_brief(candidate.overall, match.relevance)
        if match.reasons:
            candidate.reason = (candidate.reason + " " + " ".join(match.reasons[:2])).strip()
        for warning in match.warnings:
            if warning not in candidate.features.warnings:
                candidate.features.warnings.append(warning)

    survivors = [candidate for candidate in ranked if not (candidate.brief or {}).get("blocked")]
    survivors.sort(key=lambda item: item.overall, reverse=True)
    return survivors, blocked


def attach_hooks(
    candidates: list[ScoredCandidate],
    model: TranscriptModel,
    brief: ClippingBrief,
) -> list[str]:
    """Select a real hook for each kept candidate; never invent a line."""
    warnings: list[str] = []
    max_words = max(6, min(16, brief.hook.max_ms // 220))
    for candidate in candidates:
        options = build_hook_candidates(
            model,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            policy=brief.hook,
            main_text=candidate.excerpt,
            max_words=max_words,
            preference=brief.editorial.hook_preference,
        )
        candidate.hook_candidates = [option.to_dict() for option in options]
        hook = options[0] if options else build_hook(
            model,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            policy=brief.hook,
            main_text=candidate.excerpt,
            max_words=max_words,
            preference=brief.editorial.hook_preference,
        )
        candidate.hook_detail = hook.to_dict()
        candidate.hook = hook.text if hook.usable else ""
        candidate.summary = summarise(candidate.excerpt)
        if hook.source == "none":
            message = hook.reason
            candidate.features.warnings.append(message)
            if brief.hook.required:
                warnings.append(f"{candidate.title}: {message}")
        elif hook.warnings:
            candidate.features.warnings.extend(hook.warnings)
    return warnings


def attach_editorial(
    candidates: list[ScoredCandidate],
    model: TranscriptModel,
    brief: ClippingBrief,
) -> list[str]:
    """Plan retention, strategy and the source-to-output timeline per candidate.

    Plans are stored verbatim. A render reads the same plan, so the preview cannot
    claim a strategy the export does not apply.
    """
    warnings: list[str] = []
    for candidate in candidates:
        plan = build_retention_plan(
            model,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            excerpt=candidate.excerpt,
            pacing=brief.pacing,
            hook_policy=brief.hook,
            hook=candidate.hook_detail,
            hook_candidates=candidate.hook_candidates,
            editorial=brief.editorial,
            duration_preset=brief.duration_preset,
        )
        candidate.editorial = plan.model_dump()
        for warning in plan.warnings:
            if warning not in candidate.features.warnings:
                candidate.features.warnings.append(warning)
        _apply_actual_duration(candidate, brief)
        if plan.review_required and plan.editorial and plan.editorial.reordered:
            warnings.append(
                f"{candidate.title}: the {plan.editorial.label} edit needs review before export."
            )
    return warnings


def _apply_actual_duration(candidate: ScoredCandidate, brief: ClippingBrief) -> None:
    """Report the duration of the edit that will render, not just the source window.

    Pacing removes silence and a cold open repeats a grounded hook, so the output
    can differ from the candidate's original span. The duration record is updated
    from the planned timeline before selection warnings are calculated.
    """
    if not candidate.duration:
        return
    timeline = (candidate.editorial or {}).get("timeline") or {}
    actual = int(timeline.get("total_output_ms") or 0)
    if actual <= 0:
        actual = int(candidate.duration_ms)
    duration_range = brief.duration_range()
    record = duration_record(duration_range, actual)
    generated = candidate.duration.get("generated_ms")
    candidate.duration.update(record)
    if generated is not None:
        candidate.duration["generated_ms"] = generated
    status = record["status"]
    if status != "fit":
        reason = record["reason"]
        if reason not in candidate.features.warnings:
            candidate.features.warnings.append(reason)


def apply_corrections(segments: list[dict], corrections: list[dict]) -> list[dict]:
    """Return a copy of transcript segments with corrected words applied.

    Corrections are addressed by the global word index across the flattened
    transcript, which is what the UI shows. The returned copy keeps the original
    timestamps of words that were not corrected, so captions stay aligned.
    """
    if not corrections:
        return segments
    by_index = {int(item["word_index"]): item for item in corrections}
    index = 0
    result: list[dict] = []
    for segment in segments:
        clone = {**segment, "words": []}
        for word in segment.get("words", []):
            replacement = by_index.get(index)
            updated = dict(word)
            if replacement:
                if replacement.get("text"):
                    updated["word"] = replacement["text"]
                if replacement.get("start_ms") is not None:
                    updated["start_ms"] = int(replacement["start_ms"])
                if replacement.get("end_ms") is not None:
                    updated["end_ms"] = int(replacement["end_ms"])
            clone["words"].append(updated)
            index += 1
        result.append(clone)
    return result
