"""Glue between the stored brief, the ranking pass and the hook selector."""

from __future__ import annotations

from clipforge.briefs.match import blend_with_brief, evaluate_brief
from clipforge.briefs.spec import ClippingBrief, brief_from_legacy
from clipforge.db.repositories import Repositories
from clipforge.errors import NotFound, ValidationFailed
from clipforge.hooks.select import build_hook
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


def resolve_brief(
    repos: Repositories,
    project: dict,
    request: dict,
) -> tuple[ClippingBrief, str]:
    """Pick the most specific valid brief and report where it came from."""
    profile = project_profile(repos, project)
    candidates: list[tuple[str, dict]] = []
    if isinstance(request.get("brief"), dict):
        candidates.append(("request", request["brief"]))
    if project.get("brief"):
        candidates.append(("project", project["brief"]))
    if profile and (profile.get("document") or {}).get("brief"):
        candidates.append(("profile", profile["document"]["brief"]))
    candidates.append((
        "legacy",
        brief_from_legacy(
            str(request.get("goal") or "best_moments"),
            int(request.get("clip_count") or 8),
            str(request.get("duration_band") or "auto"),
            instructions=request.get("instructions"),
        ).model_dump(),
    ))

    problems: list[str] = []
    for source, payload in candidates:
        try:
            return ClippingBrief.model_validate(payload), source
        except ValueError as exc:
            problems.append(f"{source}: {exc}")
    raise ValidationFailed(
        "The clipping brief is not valid. " + "; ".join(problems[:2]),
        code="invalid_brief",
    )


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
        hook = build_hook(
            model,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            policy=brief.hook,
            main_text=candidate.excerpt,
            max_words=max_words,
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
