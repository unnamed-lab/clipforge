"""Optional LLM enrichment for grounded clip metadata.

The LLM may improve phrasing, but it is never trusted with facts. If the draft
contains a number, name or claim that is not present in the clip transcript (or
in the user's own topic/profile settings), the whole draft is rejected and the
deterministic generator is used instead.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from clipforge.packaging.brief import build_content_brief
from clipforge.packaging.generator import PackagingContext, _caption, _measure, _trim, _validate, clip_text
from clipforge.packaging.models import (
    ClipMetadata,
    ContentBrief,
    DescriptionVariants,
    HashtagSuggestion,
    KeywordResearchResult,
    PackagingProvenance,
    PlatformCaption,
    SearchKeyword,
    ThumbnailText,
)
from clipforge.packaging import director
from clipforge.packaging.skill import PROMPT_VERSION, SKILL_VERSION, SYSTEM_PROMPT, build_user_prompt
from clipforge.platforms import packaging_profile, packaging_profiles
from clipforge.providers.llm.openai_compat import OpenAiCompatibleScorer

logger = logging.getLogger(__name__)

PROPER_NOUN_ALLOW = {
    "youtube", "tiktok", "instagram", "reels", "shorts", "clipforge", "the", "this", "that",
    "these", "those", "here", "there", "what", "when", "where", "why", "how", "follow", "watch",
    "listen", "comment", "share", "save", "link", "profile", "full", "more", "new", "one",
    "find", "get", "join", "try", "tap", "click", "see", "learn", "read", "grab", "check",
    "discover", "download", "subscribe",
}


def _llm_user_prompt(
    context: PackagingContext,
    brief: ContentBrief,
    research: KeywordResearchResult | None = None,
    *,
    title_angle: str | None = None,
    more_titles: int = 0,
) -> tuple[str, str]:
    return (
        SYSTEM_PROMPT,
        build_user_prompt(
            brief,
            platform=context.platform,
            settings=context.settings,
            research=research,
            title_angle=title_angle,
            more_titles=more_titles,
        ),
    )


def _normalise_hashtag(value: str) -> str:
    cleaned = "".join(character for character in str(value or "") if character.isalnum())
    return f"#{cleaned}" if cleaned else ""


def _grounding_issues(value: str, source_lower: str, allowed: set[str]) -> list[str]:
    issues: list[str] = []
    for token in re.findall(r"\b[A-Z][a-z]{2,}\b", value or ""):
        lower = token.lower()
        if lower in PROPER_NOUN_ALLOW or lower in allowed:
            continue
        if lower not in source_lower:
            issues.append(f"Proper noun '{token}' is not present in the clip transcript.")
    return issues


def generate_metadata_llm(
    context: PackagingContext,
    provider: OpenAiCompatibleScorer,
    brief: ContentBrief | None = None,
    research: KeywordResearchResult | None = None,
    *,
    title_angle: str | None = None,
    more_titles: int = 0,
) -> tuple[ClipMetadata | None, list[str]]:
    """Return validated LLM metadata, or ``None`` plus the reason it was rejected."""
    available, reason = provider.available()
    if not available:
        return None, [reason or "No LLM endpoint configured"]
    if brief is None:
        brief = build_content_brief(
            context.transcript,
            context.candidate,
            settings=context.settings,
            platform=context.platform,
        )
    system_prompt, user_prompt = _llm_user_prompt(
        context, brief, research, title_angle=title_angle, more_titles=more_titles
    )
    try:
        raw = provider.complete_json(system_prompt, user_prompt)
    except Exception as exc:  # ProviderUnavailable is expected; never break packaging.
        logger.warning("LLM packaging failed: %s", exc)
        return None, ["The LLM provider was unavailable; deterministic metadata is shown instead."]

    text = clip_text(context.transcript, context.candidate)
    source_lower = text.full_text.lower()
    profile = packaging_profile(context.platform)
    title_limit = int(profile.get("title_max") or 100)
    recommended = int(profile.get("title_recommended") or 60)
    caption_limit = int(profile.get("caption_max") or 2_200)
    caption_unit = str(profile.get("caption_hard_limit_unit") or "characters")
    hashtag_limit = int(profile.get("hashtag_max") or 30)
    keyword_limit = int(profile.get("keyword_max") or 10)
    thumb_limit = int(profile.get("thumbnail_text_max") or 40)
    allowed = {
        str(context.topic).lower(),
        str(context.settings.get("niche") or "").lower(),
        *(str(value).lower() for value in context.subtopics),
        *(str(value).lower() for value in context.settings.get("brand_terms") or []),
    }
    allowed.discard("")
    issues: list[str] = []

    raw_titles: list[dict] = []
    for entry in (raw.get("titles") or [])[:8]:
        if not isinstance(entry, dict):
            continue
        value = _trim(str(entry.get("text") or ""), recommended)
        if len(value) < 8:
            value = _trim(str(entry.get("text") or ""), title_limit)
        if not value:
            continue
        angle = str(entry.get("angle") or "direct")
        raw_titles.append(
            {
                "text": value,
                "angle": angle,
                "rationale": _trim(str(entry.get("rationale") or entry.get("reason") or "LLM draft grounded in the source brief."), 240),
                "evidence_ids": [str(item) for item in entry.get("evidence_ids") or []],
                "limitations": [str(item)[:160] for item in entry.get("limitations") or []][:3],
            }
        )
    scored_titles = director.score_titles(
        raw_titles,
        brief=brief,
        clip_full_text=text.full_text,
        clip_sentences=[
            {"text": sentence.text, "start_ms": sentence.start_ms, "end_ms": sentence.end_ms}
            for sentence in text.sentences
        ],
        platform=context.platform,
        settings={**context.settings, "platform": context.platform},
        research=research,
        require_clip_overlap=False,
    )
    titles = [item for item in scored_titles if item.accepted]
    rejected_titles = [item for item in scored_titles if not item.accepted]
    if not titles:
        detail = "; ".join(
            item.rejection_reason for item in rejected_titles[:3] if item.rejection_reason
        ) or "no usable titles"
        issues.append(f"The LLM draft contained no accurate usable titles ({detail}).")

    descriptions_raw = raw.get("descriptions") if isinstance(raw.get("descriptions"), dict) else {}
    short = _trim(
        str(descriptions_raw.get("short") or raw.get("description_short") or ""),
        int(profile.get("description_short_max") or 150),
    )
    long_value = _trim(
        str(descriptions_raw.get("long") or raw.get("description_long") or ""),
        int(profile.get("description_long_max") or 2_200),
    )
    caption_value = _trim(str(raw.get("caption") or ""), caption_limit, caption_unit)
    issues.extend(_grounding_issues(" ".join([short, long_value, caption_value]), source_lower, allowed))
    if not short or not long_value or not caption_value:
        issues.append("The LLM draft left a required metadata field empty.")

    hashtags: list[HashtagSuggestion] = []
    seen_tags: set[str] = set()
    for entry in (raw.get("hashtags") or [])[:hashtag_limit]:
        if isinstance(entry, dict):
            tag = _normalise_hashtag(str(entry.get("tag") or ""))
            category = str(entry.get("category") or "topic")
            reason = _trim(str(entry.get("reason") or ""), 160)
        else:
            tag = _normalise_hashtag(str(entry))
            category = "topic"
            reason = ""
        if not tag or tag.lower() in seen_tags or not 3 <= len(tag) <= 42:
            continue
        if category not in {"topic", "niche", "broad", "trending"}:
            category = "topic"
        seen_tags.add(tag.lower())
        hashtags.append(
            HashtagSuggestion(
                tag=tag,
                category=category,  # type: ignore[arg-type]
                reason=reason or "Suggested by the LLM from the clip topic.",
                provenance="transcript" if category != "trending" else "trend",
                relevance=0.8 if category in {"topic", "niche"} else 0.5,
            )
        )
    if not hashtags:
        issues.append("The LLM draft contained no usable hashtags.")

    keywords: list[SearchKeyword] = []
    seen_keywords: set[str] = set()
    for entry in (raw.get("keywords") or [])[:keyword_limit]:
        reason = "AI-generated search phrase; not verified platform data."
        if isinstance(entry, dict):
            value = _trim(str(entry.get("phrase") or ""), 90)
            reason = _trim(str(entry.get("reason") or reason), 160)
        else:
            value = _trim(str(entry), 90)
        key = value.lower().lstrip("#")
        if not value or key in seen_keywords or value.startswith("#"):
            continue
        issues.extend(_grounding_issues(value, source_lower, allowed))
        seen_keywords.add(key)
        keywords.append(SearchKeyword(phrase=value, reason=reason, source="ai_generated", verified=False))
    if not keywords and context.settings.get("include_keywords", True):
        issues.append("The LLM draft contained no usable search keywords.")

    cta_value = _trim(str(raw.get("cta") or ""), 160) or None
    if str(context.settings.get("cta_preference") or "auto") == "none":
        cta_value = None
    if cta_value:
        issues.extend(_grounding_issues(cta_value, source_lower, allowed))
    thumbnails_raw = raw.get("thumbnail_texts")
    thumbnail_values = [
        _trim(str(value), thumb_limit)
        for value in (thumbnails_raw if isinstance(thumbnails_raw, list) else [])
        if str(value).strip()
    ]
    thumb_value = _trim(str(raw.get("thumbnail_text") or (thumbnail_values[0] if thumbnail_values else "")), thumb_limit) or None
    if thumb_value:
        issues.extend(_grounding_issues(thumb_value, source_lower, allowed))

    restricted = [
        *(str(value) for value in context.settings.get("restricted_words") or []),
        *(str(value) for value in context.settings.get("restricted_topics") or []),
        *(str(value) for value in context.settings.get("restricted_claims") or []),
        *(str(value) for value in context.banned_terms or []),
    ]
    generated_text = " ".join(
        [title.text for title in titles]
        + [short, long_value, caption_value]
        + [item.tag for item in hashtags]
        + [item.phrase for item in keywords]
        + [cta_value or ""]
        + [thumb_value or ""]
    )
    for term in restricted:
        if term and term.lower() in generated_text.lower():
            issues.append(f"Restricted term or claim appears in the LLM draft: {term}")
    for number in set(re.findall(r"\b\d[\d.,]*\b", generated_text)):
        if number not in source_lower:
            issues.append(f"Number '{number}' in the LLM draft does not appear in the clip transcript.")

    selected_index = raw.get("selected_title_index")
    if isinstance(selected_index, int) and 0 <= selected_index < len(scored_titles):
        chosen = scored_titles[selected_index]
        if chosen.accepted:
            titles = [chosen, *[item for item in titles if item.text != chosen.text]]

    if issues:
        return None, list(dict.fromkeys(issues))

    now = (context.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    context_warnings = [
        *[str(value)[:200] for value in raw.get("context_warnings") or [] if isinstance(value, str)],
        *brief.ambiguity,
        *brief.warnings,
    ]
    metadata = ClipMetadata(
        titles=titles,
        descriptions=DescriptionVariants(
            short=short,
            long=long_value,
            short_limit=int(profile.get("description_short_max") or 150),
            long_limit=int(profile.get("description_long_max") or 2_200),
            within_limit=True,
        ),
        caption=PlatformCaption(
            platform=context.platform,
            label=str(profile.get("label") or context.platform),
            text=caption_value,
            char_count=_measure(caption_value, caption_unit),
            limit=caption_limit,
            within_limit=_measure(caption_value, caption_unit) <= caption_limit,
            notes=["LLM draft checked against the source brief and clip transcript."],
        ),
        platform_captions=[],
        hashtags=hashtags,
        keywords=keywords,
        cta=cta_value,
        thumbnail_text=ThumbnailText(
            text=thumb_value,
            limit=thumb_limit,
            within_limit=bool(thumb_value and len(thumb_value) <= thumb_limit),
            reason="LLM draft checked against the source brief and clip transcript.",
        )
        if thumb_value
        else None,
        thumbnail_alternatives=thumbnail_values[1:3],
        content_angle=_trim(str(raw.get("content_angle") or ""), 240),
        key_takeaway=_trim(str(raw.get("key_takeaway") or brief.practical_takeaway or brief.key_insight), 320),
        suggested_audience=_trim(str(raw.get("suggested_audience") or context.settings.get("audience") or ""), 160),
        source_hook=brief.stronger_hook,
        source_hook_start_ms=next(
            (item.start_ms for item in brief.evidence if item.id == brief.hook_evidence_id),
            int(context.candidate.get("start_ms") or 0),
        ),
        editorial_rationale=titles[0].reason if titles else "",
        context_warnings=list(dict.fromkeys(context_warnings)),
        provenance=PackagingProvenance(
            generated_at=now.isoformat(),
            method="llm",
            provider=provider.name,
            model=provider.model,
            transcript_id=context.transcript.get("id"),
            source_start_ms=text.source_start_ms,
            source_end_ms=text.source_end_ms,
            timeline_applied=text.timeline_applied,
            sentence_ids=text.sentence_ids,
            grounding=[
                f"Editorial skill {SKILL_VERSION} ({PROMPT_VERSION}) applied to a {brief.version} source brief.",
                f"The brief covered {len(brief.sections)} sections; {brief.omitted_sections} were omitted from the prompt.",
                "LLM draft checked against clip transcript words and the brief evidence.",
                "Numbers and proper nouns must appear in the transcript or user-supplied settings.",
                "Rejected title candidates were not returned to the user.",
                "No trend or search-volume metric was generated by the LLM.",
            ],
            warnings=list(
                dict.fromkeys(
                    [
                        *text.warnings,
                        *brief.warnings,
                        *(
                            [f"Rejected an inaccurate title candidate: {item.text} ({item.rejection_reason})" for item in rejected_titles[:3]]
                        ),
                        *(research.limitations[:2] if research else []),
                    ]
                )
            ),
        ),
    )
    selected_caption = metadata.caption
    metadata.platform_captions = [
        selected_caption if name == context.platform
        else _caption(name, text, "", str(context.topic or brief.clip_topic or ""), hashtags, cta_value)
        for name in packaging_profiles()
    ]
    metadata.validation = _validate(
        metadata,
        context=context,
        text=text,
        topic=str(context.topic or brief.clip_topic or ""),
    )
    if not metadata.validation.ok:
        return None, metadata.validation.issues
    return metadata, list(text.warnings)
