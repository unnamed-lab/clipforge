from __future__ import annotations

import copy
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from clipforge.api.deps import get_state
from clipforge.api.models import PackagingGenerateBody, PackagingPatchBody, TrendCheckBody
from clipforge.briefs.spec import ClippingBrief
from clipforge.errors import NotFound, ValidationFailed
from clipforge.packaging.brief import brief_key, build_content_brief
from clipforge.packaging.generator import (
    PackagingContext,
    _hashtag,
    _measure,
    _validate,
    clip_text,
    generate_metadata,
)
from clipforge.packaging.llm import generate_metadata_llm
from clipforge.packaging.models import (
    ClipMetadata,
    HashtagSuggestion,
    ManualTrend,
    PackagingDocument,
    TrendRecommendation,
)
from clipforge.packaging.research.service import KeywordResearchService, build_research_registry
from clipforge.packaging.skill import SKILL_VERSION, skill_manifest, title_weights
from clipforge.packaging.trend.base import TrendQuery
from clipforge.packaging.trend.ranking import merged_weights
from clipforge.packaging.trend.registry import build_trend_provider_registry
from clipforge.packaging.trend.service import TrendService
from clipforge.platforms import packaging_profiles
from clipforge.profiles.spec import PackagingPreferences, resolve_settings
from clipforge.providers.llm.openai_compat import build_llm_provider

router = APIRouter(prefix="/api/projects", tags=["packaging"])


def _effective_settings(state, project: dict) -> dict:
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    packaging = resolved["settings"].get("packaging") or {}
    return PackagingPreferences.model_validate(packaging).model_dump()


def _merge_settings(base: dict, patch: dict | None) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (patch or {}).items():
        if value is None:
            continue
        merged[key] = value
    try:
        return PackagingPreferences.model_validate(merged).model_dump()
    except ValueError as exc:
        raise ValidationFailed(f"Packaging settings are not valid: {exc}", code="invalid_settings") from exc


def _brief_for(state, project: dict) -> ClippingBrief:
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    raw = project.get("brief") or resolved["settings"].get("brief") or {}
    try:
        return ClippingBrief.model_validate(raw) if raw else ClippingBrief()
    except ValueError as exc:
        raise ValidationFailed(f"The stored brief is not valid: {exc}", code="invalid_brief") from exc


def _rules(state, project: dict) -> dict:
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    rules = copy.deepcopy(((profile or {}).get("document") or {}).get("rules") or {})
    rules.update((project.get("overrides") or {}).get("rules") or {})
    return rules


def _source_meta(state, project: dict) -> tuple[str, str]:
    title = ""
    description = ""
    source_id = project.get("library_source_id")
    if source_id:
        try:
            row = state.repos.library.get(source_id)
        except Exception:  # noqa: BLE001 - optional provenance must never block packaging
            row = None
        if row:
            title = str(row.get("title") or "")
            description = str(row.get("description") or "")
    goals = project.get("goals") or {}
    title = title or str(goals.get("source_title") or "")
    description = description or str(goals.get("source_description") or "")
    return title[:300], description[:1000]


def _brief(state, project: dict, candidate: dict, *, settings: dict, platform: str):
    transcript = state.repos.transcripts.latest(project["id"]) or {}
    title, description = _source_meta(state, project)
    return build_content_brief(
        transcript,
        candidate,
        source_title=title or str(project.get("name") or ""),
        source_description=description or None,
        settings=settings,
        platform=platform,
    )


def _context(state, project: dict, candidate: dict, *, platform: str, settings: dict, genre: str, strategy: str,
             topic: str = "", subtopics: list[str] | None = None) -> PackagingContext:
    transcript = state.repos.transcripts.latest(project["id"]) or {}
    rules = _rules(state, project)
    title, description = _source_meta(state, project)
    return PackagingContext(
        transcript=transcript,
        candidate=candidate,
        platform=platform,
        settings=settings,
        genre=genre or "auto",
        strategy=strategy or "chronological",
        topic=topic,
        subtopics=list(subtopics or []),
        banned_terms=list(rules.get("banned_terms") or []),
        source_title=title,
        source_description=description,
    )


def _document(candidate: dict, settings: dict) -> PackagingDocument:
    raw = candidate.get("packaging") or {}
    if not raw:
        return PackagingDocument(platform=str(settings.get("default_platform") or "cross_platform"), settings=settings)
    try:
        document = PackagingDocument.model_validate(raw)
    except ValueError:
        document = PackagingDocument(platform=str(settings.get("default_platform") or "cross_platform"), settings=settings)
    if not document.settings:
        document.settings = settings
    if document.platform not in packaging_profiles():
        document.platform = str(settings.get("default_platform") or "cross_platform")
    return document


def _save(state, project_id: str, candidate_id: str, document: PackagingDocument) -> dict:
    document.updated_at = datetime.now(timezone.utc).isoformat()
    state.repos.candidates.set_packaging(project_id, candidate_id, document.model_dump())
    state.repos.projects.touch(project_id)
    return document.model_dump()


def _trend_service(state) -> TrendService:
    registry = build_trend_provider_registry(state.settings)
    return TrendService(cache=state.repos.trend_cache, registry=registry, settings=state.settings)


def _research_service(state) -> KeywordResearchService:
    trend_registry = build_trend_provider_registry(state.settings)
    registry = build_research_registry(state.settings, trend_registry.get("youtube"))
    return KeywordResearchService(
        cache=state.repos.trend_cache,
        registry=registry,
        settings=state.settings,
    )


EDITABLE_FIELDS = ("titles", "description", "caption", "hashtags", "keywords", "thumbnail", "cta")


def _detect_edited_fields(existing: ClipMetadata, updated: ClipMetadata) -> list[str]:
    changed: list[str] = []
    if [item.text for item in existing.titles] != [item.text for item in updated.titles]:
        changed.append("titles")
    if existing.descriptions.model_dump() != updated.descriptions.model_dump():
        changed.append("description")
    if existing.caption.text != updated.caption.text:
        changed.append("caption")
    if [item.tag for item in existing.hashtags] != [item.tag for item in updated.hashtags]:
        changed.append("hashtags")
    if [item.phrase for item in existing.keywords] != [item.phrase for item in updated.keywords]:
        changed.append("keywords")
    if (existing.thumbnail_text.text if existing.thumbnail_text else "") != (
        updated.thumbnail_text.text if updated.thumbnail_text else ""
    ):
        changed.append("thumbnail")
    if (existing.cta or "") != (updated.cta or ""):
        changed.append("cta")
    return changed


def _preserve_fields(
    generated: ClipMetadata,
    existing: ClipMetadata | None,
    fields: set[str],
    *,
    platform_changed: bool,
) -> ClipMetadata:
    """Keep approved field values while allowing other fields to regenerate."""
    if existing is None or not fields:
        return generated
    preserved = generated.model_copy(deep=True)
    if "titles" in fields and existing.titles:
        preserved.titles = [item.model_copy(deep=True) for item in existing.titles]
        preserved.editorial_rationale = existing.editorial_rationale
    if "description" in fields:
        preserved.descriptions = existing.descriptions.model_copy(deep=True)
    if "caption" in fields and not platform_changed:
        preserved.caption = existing.caption.model_copy(deep=True)
        preserved.platform_captions = [item.model_copy(deep=True) for item in existing.platform_captions]
    if "hashtags" in fields:
        preserved.hashtags = [item.model_copy(deep=True) for item in existing.hashtags]
    if "keywords" in fields:
        preserved.keywords = [item.model_copy(deep=True) for item in existing.keywords]
    if "thumbnail" in fields:
        preserved.thumbnail_text = (
            existing.thumbnail_text.model_copy(deep=True) if existing.thumbnail_text else None
        )
        preserved.thumbnail_alternatives = list(existing.thumbnail_alternatives)
    if "cta" in fields:
        preserved.cta = existing.cta
    preserved.validation.warnings.append(
        "Approved fields were preserved; regenerate a specific field to replace it."
    )
    return preserved


def _query(
    *,
    state,
    project: dict,
    candidate: dict,
    document: PackagingDocument,
    platform: str,
    settings: dict,
    genre: str,
    strategy: str,
    topic: str = "",
    subtopics: list[str] | None = None,
    region: str | None = None,
    country: str | None = None,
    language: str | None = None,
    niche: str | None = None,
    audience: str | None = None,
    freshness_days: int | None = None,
    limit: int | None = None,
) -> TrendQuery:
    transcript = state.repos.transcripts.latest(project["id"]) or {}
    metadata = document.metadata
    title = (
        metadata.titles[0].text
        if metadata and metadata.titles
        else str(candidate.get("title") or candidate.get("hook") or "")
    )
    description = ""
    if metadata:
        description = metadata.descriptions.long or metadata.descriptions.short
    return TrendQuery(
        transcript=(clip_text(transcript, candidate).full_text if transcript else str(candidate.get("excerpt") or ""))[:4000],
        title=title,
        description=description,
        topic=topic or str(settings.get("niche") or "") or title[:120],
        subtopics=list(subtopics or []),
        genre=genre,
        platform=platform,
        region=region or str(settings.get("region") or "US"),
        country=country or str(settings.get("region") or "US"),
        language=language or str(settings.get("language") or "en"),
        niche=niche if niche is not None else str(settings.get("niche") or ""),
        audience=audience if audience is not None else str(settings.get("audience") or ""),
        freshness_days=freshness_days or 30,
        limit=limit or 8,
    )


def _trend_by_id(document: PackagingDocument, trend_id: str | None) -> TrendRecommendation:
    if not trend_id or not document.trend:
        raise NotFound("Trend recommendation not found")
    for trend in [*document.trend.results, *document.trend.manual_references, *document.saved_trends]:
        if trend.id == trend_id:
            return trend
    raise NotFound("Trend recommendation not found")


def _apply_trend_hashtag(metadata: ClipMetadata, trend: TrendRecommendation, platform: str) -> ClipMetadata:
    profile = packaging_profiles().get(platform) or packaging_profiles()["cross_platform"]
    max_tags = int(profile.get("hashtag_max") or 30)
    words = [word for word in trend.name.split() if word.isalnum()][:3]
    tag = _hashtag("".join(words))
    if not tag:
        return metadata
    existing = {item.tag.lower() for item in metadata.hashtags}
    if tag.lower() in existing:
        return metadata
    if len(metadata.hashtags) >= max_tags:
        return metadata
    metadata.hashtags.append(
        HashtagSuggestion(
            tag=tag,
            category="trending",
            reason=(
                f"Selected from {trend.provider} evidence: {trend.evidence[:180]}"
                if trend.evidence
                else f"Selected from {trend.provider}."
            ),
            provenance="trend",
            relevance=trend.scores.weighted,
        )
    )
    caption = metadata.caption
    if caption:
        unit = str(
            (packaging_profiles().get(caption.platform) or {}).get("caption_hard_limit_unit")
            or "characters"
        )
        attempt = f"{caption.text} {tag}".strip()
        if _measure(attempt, unit) <= caption.limit:
            caption.text = attempt
            caption.char_count = _measure(attempt, unit)
            caption.within_limit = caption.char_count <= caption.limit
            for item in metadata.platform_captions:
                if item.platform == caption.platform:
                    item.text = caption.text
                    item.char_count = caption.char_count
                    item.within_limit = caption.within_limit
    metadata.validation.warnings.append(
        "A trend hashtag was added to the caption and hashtag list. The video and its factual description were not changed."
    )
    return metadata


@router.get("/{project_id}/candidates/{candidate_id}/packaging")
def get_packaging(project_id: str, candidate_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    candidate = state.repos.candidates.get(project_id, candidate_id)
    settings = _effective_settings(state, project)
    document = _document(candidate, settings)
    return {"candidate": candidate, "packaging": document.model_dump()}


@router.post("/{project_id}/candidates/{candidate_id}/packaging")
def generate_packaging(
    project_id: str, candidate_id: str, payload: PackagingGenerateBody, request: Request
) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    candidate = state.repos.candidates.get(project_id, candidate_id)
    if not state.repos.transcripts.latest(project_id) and not candidate.get("excerpt"):
        raise ValidationFailed("Analyse this project before generating packaging", code="no_transcript")
    settings = _merge_settings(_effective_settings(state, project), payload.settings)
    platform = payload.platform or str(settings.get("default_platform") or "cross_platform")
    if platform not in packaging_profiles():
        raise ValidationFailed("Unknown packaging platform", code="invalid_platform")
    brief_config = _brief_for(state, project)
    editorial = brief_config.editorial
    genre = payload.genre or editorial.genre or "auto"
    stored_strategy = str((candidate.get("editorial") or {}).get("strategy") or "")
    strategy = payload.strategy or stored_strategy or editorial.preferred_strategy or "chronological"
    topic = payload.topic or str(settings.get("niche") or "")
    document = _document(candidate, settings)
    platform_changed = document.platform != platform

    transcript = state.repos.transcripts.latest(project["id"]) or {}
    title, description = _source_meta(state, project)
    cache_key = brief_key(transcript, candidate, source_title=title or str(project.get("name") or ""))
    if (
        payload.reuse_brief
        and document.brief is not None
        and document.brief.brief_key == cache_key
    ):
        brief = document.brief
        brief_reused = True
    else:
        brief = _brief(state, project, candidate, settings=settings, platform=platform)
        brief_reused = False

    research_mode = payload.research_mode or str(settings.get("research_mode") or "off")
    research = _research_service(state).research(
        brief,
        mode=research_mode,
        manual_terms=payload.manual_terms,
        region=str(settings.get("region") or "US"),
        language=str(settings.get("language") or "en"),
        force=bool(payload.check_trend and payload.force_trend),
    )

    context = _context(
        state, project, candidate,
        platform=platform, settings=settings, genre=genre, strategy=strategy,
        topic=topic, subtopics=payload.subtopics,
    )
    metadata = generate_metadata(context, brief, research)
    warnings: list[str] = []
    if payload.use_llm:
        provider = build_llm_provider(state.settings)
        if provider is None:
            warnings.append("No LLM endpoint is configured; deterministic metadata is shown.")
        else:
            llm_metadata, llm_warnings = generate_metadata_llm(
                context,
                provider,
                brief,
                research,
                title_angle=payload.title_angle,
                more_titles=payload.more_titles,
            )
            if llm_metadata is not None:
                metadata = llm_metadata
            else:
                warnings.extend(llm_warnings)
    if brief_reused:
        warnings.append("Reused the cached source brief; only packaging was regenerated.")
    if brief.omitted_sections:
        warnings.append(
            f"The brief omitted {brief.omitted_sections} source sections from the prompt; transcript evidence is still available for validation."
        )
    if research.mode != "off" and research.status in {"unavailable", "manual_required", "auth_missing", "quota_exhausted", "rate_limited"}:
        warnings.append(
            f"Research was {research.status.replace('_', ' ')}; suggestions remain source-based and no metric was invented."
        )

    explicit_request = bool(payload.fields) and "all" not in payload.fields
    if explicit_request:
        requested = {value for value in payload.fields if value in EDITABLE_FIELDS}
        preserve = set(EDITABLE_FIELDS) - requested
    else:
        requested = set(EDITABLE_FIELDS)
        preserve = (set(document.edited_fields) | set(payload.edited_fields)) - set(payload.force_fields)
    existing = document.metadata
    if payload.metadata:
        try:
            existing = ClipMetadata.model_validate(payload.metadata)
        except ValueError as exc:
            raise ValidationFailed(f"Packaging metadata is not valid: {exc}", code="invalid_metadata") from exc
    metadata = _preserve_fields(metadata, existing, preserve, platform_changed=platform_changed)

    regenerated = requested - preserve
    edited_fields = (set(document.edited_fields) | set(payload.edited_fields)) - regenerated
    edited_fields |= {field for field in payload.force_fields if field in EDITABLE_FIELDS}
    metadata.provenance.warnings.extend(
        value for value in warnings if value not in metadata.provenance.warnings
    )
    metadata.validation.warnings.extend(
        value for value in warnings if value not in metadata.validation.warnings
    )

    document.settings = settings
    document.platform = platform
    document.metadata = metadata
    document.brief = brief
    document.research = research
    document.skill_version = SKILL_VERSION
    document.edited_fields = sorted(field for field in edited_fields if field in EDITABLE_FIELDS)
    document.brief_meta = {
        "version": brief.version,
        "cached": brief_reused,
        "omitted_sections": brief.omitted_sections,
        "confidence": brief.confidence,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    document.warnings = list(dict.fromkeys([*document.warnings, *warnings]))
    trend_mode = str(settings.get("trend_check") or "manual")
    if (payload.check_trend or trend_mode == "automatic") and trend_mode != "disabled":
        query = _query(
            state=state, project=project, candidate=candidate, document=document,
            platform=platform, settings=settings, genre=genre, strategy=strategy, topic=topic,
            subtopics=payload.subtopics,
        )
        service = _trend_service(state)
        trend = service.check(
            query,
            dismissed_ids=set(document.dismissed_trend_ids),
            force=payload.force_trend,
        )
        document.trend = service.with_manual_references(
            trend, document.manual_trends, query, dismissed_ids=set(document.dismissed_trend_ids)
        )
    saved = _save(state, project_id, candidate_id, document)
    return {"candidate": state.repos.candidates.get(project_id, candidate_id), "packaging": saved}


@router.patch("/{project_id}/candidates/{candidate_id}/packaging")
def patch_packaging(
    project_id: str, candidate_id: str, payload: PackagingPatchBody, request: Request
) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    candidate = state.repos.candidates.get(project_id, candidate_id)
    settings = _effective_settings(state, project)
    document = _document(candidate, settings)

    if payload.settings is not None:
        document.settings = _merge_settings(document.settings or settings, payload.settings)
    if payload.platform:
        if payload.platform not in packaging_profiles():
            raise ValidationFailed("Unknown packaging platform", code="invalid_platform")
        document.platform = payload.platform
    if payload.metadata is not None:
        try:
            metadata = ClipMetadata.model_validate(payload.metadata)
        except ValueError as exc:
            raise ValidationFailed(f"Packaging metadata is not valid: {exc}", code="invalid_metadata") from exc
        previous = document.metadata
        edited = set(document.edited_fields) | set(payload.edited_fields)
        if previous is not None:
            edited |= set(_detect_edited_fields(previous, metadata))
        document.edited_fields = sorted(field for field in edited if field in EDITABLE_FIELDS)
        context = _context(
            state, project, candidate,
            platform=document.platform,
            settings=document.settings or settings,
            genre=str((candidate.get("editorial") or {}).get("strategy") or "auto"),
            strategy=str((candidate.get("editorial") or {}).get("strategy") or "chronological"),
            topic=str((document.settings or {}).get("niche") or ""),
        )
        text = clip_text(state.repos.transcripts.latest(project_id) or {}, candidate)
        metadata.validation = _validate(metadata, context=context, text=text, topic=str((document.settings or {}).get("niche") or ""))
        document.metadata = metadata

    trend = _trend_by_id(document, payload.trend_id) if payload.trend_action in {"use", "dismiss", "save"} else None
    if payload.trend_action == "dismiss" and payload.trend_id:
        if payload.trend_id not in document.dismissed_trend_ids:
            document.dismissed_trend_ids.append(payload.trend_id)
        if document.trend:
            document.trend.results = [item for item in document.trend.results if item.id != payload.trend_id]
            document.trend.manual_references = [
                item for item in document.trend.manual_references if item.id != payload.trend_id
            ]
    elif payload.trend_action == "restore" and payload.trend_id:
        document.dismissed_trend_ids = [value for value in document.dismissed_trend_ids if value != payload.trend_id]
    elif payload.trend_action == "save" and trend is not None:
        if all(item.id != trend.id for item in document.saved_trends):
            document.saved_trends.append(trend)
    elif payload.trend_action == "use" and trend is not None:
        if document.trend:
            for item in [*document.trend.results, *document.trend.manual_references]:
                if item.id == trend.id:
                    item.used = True
                    item.dismissed = False
            document.trend.results = [item for item in document.trend.results if item.id != trend.id] + [
                item for item in document.trend.results if item.id == trend.id
            ]
        if payload.regenerate_with_trend and document.metadata is not None:
            document.metadata = _apply_trend_hashtag(document.metadata, trend, document.platform)

    if payload.trend_action == "manual":
        try:
            manual = ManualTrend.model_validate(payload.manual_trend or {})
        except ValueError as exc:
            raise ValidationFailed(f"Manual trend reference is not valid: {exc}", code="invalid_trend") from exc
        existing = {(item.name.lower(), (item.source_url or "").lower()) for item in document.manual_trends}
        if (manual.name.lower(), (manual.source_url or "").lower()) not in existing:
            document.manual_trends.append(manual)
        brief = _brief_for(state, project)
        genre = brief.editorial.genre or "auto"
        query = _query(
            state=state, project=project, candidate=candidate, document=document,
            platform=document.platform, settings=document.settings or settings, genre=genre,
            strategy=str((candidate.get("editorial") or {}).get("strategy") or "chronological"),
            topic=str((document.settings or {}).get("niche") or ""),
        )
        service = _trend_service(state)
        if document.trend is None:
            from clipforge.packaging.models import TrendCheckResult

            document.trend = TrendCheckResult(
                status="manual_required",
                provider="manual",
                platform=document.platform,
                checked_at=datetime.now(timezone.utc).isoformat(),
                settings=query.settings(),
                notes=["Manual reference only. No official provider was queried."],
                limitations=["No provider verified this reference or its metrics."],
            )
        document.trend = service.with_manual_references(
            document.trend, document.manual_trends, query, dismissed_ids=set(document.dismissed_trend_ids)
        )

    saved = _save(state, project_id, candidate_id, document)
    return {"candidate": state.repos.candidates.get(project_id, candidate_id), "packaging": saved}


@router.post("/{project_id}/candidates/{candidate_id}/packaging/trend-check")
def check_trends(
    project_id: str, candidate_id: str, payload: TrendCheckBody, request: Request
) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    candidate = state.repos.candidates.get(project_id, candidate_id)
    settings = _merge_settings(_effective_settings(state, project), payload.settings)
    document = _document(candidate, settings)
    platform = payload.platform or document.platform or str(settings.get("default_platform") or "cross_platform")
    if platform not in packaging_profiles():
        raise ValidationFailed("Unknown packaging platform", code="invalid_platform")
    if str(settings.get("trend_check") or "manual") == "disabled" and not payload.force:
        raise ValidationFailed("Trend checking is disabled for this profile", code="trend_disabled")
    brief = _brief_for(state, project)
    genre = payload.genre or brief.editorial.genre or "auto"
    strategy = payload.strategy or str((candidate.get("editorial") or {}).get("strategy") or "chronological")
    query = _query(
        state=state, project=project, candidate=candidate, document=document,
        platform=platform, settings=settings, genre=genre, strategy=strategy,
        topic=payload.topic or str(settings.get("niche") or ""), subtopics=payload.subtopics,
        region=payload.region, country=payload.country, language=payload.language,
        niche=payload.niche, audience=payload.audience,
        freshness_days=payload.freshness_days, limit=payload.limit,
    )
    service = _trend_service(state)
    trend = service.check(
        query,
        dismissed_ids=set(document.dismissed_trend_ids),
        weights=payload.weights,
        force=payload.force,
    )
    document.settings = settings
    document.platform = platform
    document.trend = service.with_manual_references(
        trend, document.manual_trends, query,
        dismissed_ids=set(document.dismissed_trend_ids), weights=payload.weights,
    )
    saved = _save(state, project_id, candidate_id, document)
    return {"candidate": state.repos.candidates.get(project_id, candidate_id), "packaging": saved}


@router.get("/{project_id}/packaging/config")
def packaging_config(project_id: str, request: Request) -> dict:
    state = get_state(request)
    project = state.repos.projects.get(project_id)
    profile = state.repos.profiles.get(project["profile_id"]) if project.get("profile_id") else None
    resolved = resolve_settings(profile, project.get("overrides"))
    service = _trend_service(state)
    return {
        "settings": resolved["settings"].get("packaging") or {},
        "provenance": resolved["provenance"],
        "platforms": packaging_profiles(),
        "providers": service.config(),
        "research": _research_service(state).config(),
        "skill": skill_manifest(),
        "title_weights": title_weights(),
        "trend_weights": merged_weights(),
        "notes": [
            "Metadata limits and provider capabilities are configuration, not live guarantees.",
            "No trend metric is shown unless a recorded provider response supplied it.",
            "Packaging works without any research provider or API key.",
        ],
    }
