"""Optional LLM enrichment for grounded clip metadata.

The LLM may improve phrasing, but it is never trusted with facts. If the draft
contains a number, name or claim that is not present in the clip transcript (or
in the user's own topic/profile settings), the whole draft is rejected and the
deterministic generator is used instead.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from clipforge.packaging.generator import PackagingContext, _caption, _measure, _trim, _validate, clip_text
from clipforge.packaging.models import (
    ClipMetadata,
    DescriptionVariants,
    HashtagSuggestion,
    PackagingProvenance,
    PlatformCaption,
    SearchKeyword,
    ThumbnailText,
    TitleVariant,
)
from clipforge.platforms import packaging_profile, packaging_profiles
from clipforge.providers.llm.openai_compat import OpenAiCompatibleScorer

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You write social metadata for a short clip cut from a longer spoken-word video.
You may only use facts present in the supplied clip transcript and the user's own topic/profile settings.
Never invent a name, place, statistic, quotation, promise, product claim or trend metric.
Never write that something is viral, guaranteed, a must-see, or about to trend.
Return STRICT JSON, no markdown, matching this shape:
{"titles":[{"text":"...","style":"direct|curiosity|question|result|how_to|quote","reason":"..."}],
 "description_short":"...","description_long":"...","caption":"...",
 "hashtags":[{"tag":"#example","category":"topic|niche|broad|trending","reason":"..."}],
 "keywords":["natural language search phrase"],
 "cta":"... or empty","thumbnail_text":"..."}
Use the exact limits supplied. Keep hashtags small and relevant. Keywords must be phrases, not hashtags.
Do not add a fact that is not in the transcript. If a field cannot be supported, return an empty string."""

PROPER_NOUN_ALLOW = {
    "youtube", "tiktok", "instagram", "reels", "shorts", "clipforge", "the", "this", "that",
    "these", "those", "here", "there", "what", "when", "where", "why", "how", "follow", "watch",
    "listen", "comment", "share", "save", "link", "profile", "full", "more", "new", "one",
    "find", "get", "join", "try", "tap", "click", "see", "learn", "read", "grab", "check",
    "discover", "download", "subscribe",
}


def _llm_user_prompt(context: PackagingContext) -> tuple[str, str]:
    text = clip_text(context.transcript, context.candidate)
    profile = packaging_profile(context.platform)
    payload = {
        "platform": context.platform,
        "limits": {
            "title_max": profile.get("title_max"),
            "title_recommended": profile.get("title_recommended"),
            "description_short_max": profile.get("description_short_max"),
            "description_long_max": profile.get("description_long_max"),
            "caption_max": profile.get("caption_max"),
            "hashtag_max": profile.get("hashtag_max"),
            "hashtag_recommended": profile.get("hashtag_recommended"),
            "keyword_max": profile.get("keyword_max"),
            "thumbnail_text_max": profile.get("thumbnail_text_max"),
        },
        "genre": context.genre,
        "editorial_strategy": context.strategy,
        "topic": context.topic,
        "subtopics": context.subtopics,
        "settings": {
            "tone": context.settings.get("tone"),
            "audience": context.settings.get("audience"),
            "language": context.settings.get("language"),
            "region": context.settings.get("region"),
            "niche": context.settings.get("niche"),
            "brand_voice": context.settings.get("brand_voice"),
            "hashtag_style": context.settings.get("hashtag_style"),
            "hashtag_count": context.settings.get("hashtag_count"),
            "cta_preference": context.settings.get("cta_preference"),
            "cta_text": context.settings.get("cta_text"),
            "brand_terms": context.settings.get("brand_terms") or [],
            "restricted_words": context.settings.get("restricted_words") or [],
            "restricted_topics": context.settings.get("restricted_topics") or [],
            "restricted_claims": context.settings.get("restricted_claims") or [],
        },
        "clip_transcript": text.full_text[:6000],
        "clip_sentences": [sentence.text for sentence in text.sentences][:20],
        "timeline_applied": text.timeline_applied,
    }
    return SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False)


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
) -> tuple[ClipMetadata | None, list[str]]:
    """Return validated LLM metadata, or ``None`` plus the reason it was rejected."""
    available, reason = provider.available()
    if not available:
        return None, [reason or "No LLM endpoint configured"]
    system_prompt, user_prompt = _llm_user_prompt(context)
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

    titles: list[TitleVariant] = []
    seen_titles: set[str] = set()
    for entry in (raw.get("titles") or [])[:5]:
        if not isinstance(entry, dict):
            continue
        value = _trim(str(entry.get("text") or ""), recommended)
        if len(value) < 8:
            value = _trim(str(entry.get("text") or ""), title_limit)
        if not value or value.lower() in seen_titles:
            continue
        issues.extend(_grounding_issues(value, source_lower, allowed))
        style = str(entry.get("style") or "direct")
        if style not in {"direct", "curiosity", "question", "result", "how_to", "quote"}:
            style = "direct"
        seen_titles.add(value.lower())
        titles.append(
            TitleVariant(
                text=value,
                style=style,  # type: ignore[arg-type]
                reason=_trim(str(entry.get("reason") or "LLM draft grounded in the clip transcript."), 180),
                chars=len(value),
                limit=title_limit,
                within_limit=len(value) <= title_limit,
                basis="transcript",
            )
        )
    if not titles:
        issues.append("The LLM draft contained no usable titles.")

    short = _trim(str(raw.get("description_short") or ""), int(profile.get("description_short_max") or 150))
    long_value = _trim(str(raw.get("description_long") or ""), int(profile.get("description_long_max") or 2_200))
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
        value = _trim(str(entry), 90)
        key = value.lower().lstrip("#")
        if not value or key in seen_keywords or value.startswith("#"):
            continue
        issues.extend(_grounding_issues(value, source_lower, allowed))
        seen_keywords.add(key)
        keywords.append(SearchKeyword(phrase=value, reason="AI-generated search phrase; not verified platform data.", source="ai_generated", verified=False))
    if not keywords and context.settings.get("include_keywords", True):
        issues.append("The LLM draft contained no usable search keywords.")

    cta_value = _trim(str(raw.get("cta") or ""), 160) or None
    if str(context.settings.get("cta_preference") or "auto") == "none":
        cta_value = None
    if cta_value:
        issues.extend(_grounding_issues(cta_value, source_lower, allowed))
    thumb_value = _trim(str(raw.get("thumbnail_text") or ""), thumb_limit) or None
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

    if issues:
        return None, list(dict.fromkeys(issues))

    now = (context.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
            notes=["LLM draft checked against the clip transcript."],
        ),
        platform_captions=[],
        hashtags=hashtags,
        keywords=keywords,
        cta=cta_value,
        thumbnail_text=ThumbnailText(
            text=thumb_value,
            limit=thumb_limit,
            within_limit=bool(thumb_value and len(thumb_value) <= thumb_limit),
            reason="LLM draft checked against the clip transcript.",
        )
        if thumb_value
        else None,
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
                "LLM draft checked against clip transcript words.",
                "Numbers and proper nouns must appear in the transcript or user-supplied settings.",
                "No trend metric was generated by the LLM.",
            ],
            warnings=list(text.warnings),
        ),
    )
    selected_caption = metadata.caption
    metadata.platform_captions = [
        selected_caption if name == context.platform
        else _caption(name, text, "", str(context.topic or ""), hashtags, cta_value)
        for name in packaging_profiles()
    ]
    metadata.validation = _validate(metadata, context=context, text=text, topic=str(context.topic or ""))
    if not metadata.validation.ok:
        return None, metadata.validation.issues
    return metadata, list(text.warnings)
