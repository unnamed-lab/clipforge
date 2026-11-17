"""Deterministic, transcript-grounded clip metadata.

This is the offline path. It is deliberately conservative: every title,
description, caption and keyword is assembled from words that exist in the
final clip, plus the user's own topic/profile settings. Nothing is invented and
no trend metric is produced here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from clipforge.packaging.models import (
    ClipMetadata,
    DescriptionVariants,
    HashtagSuggestion,
    MetadataValidation,
    PackagingProvenance,
    PlatformCaption,
    SearchKeyword,
    ThumbnailText,
    TitleVariant,
)
from clipforge.platforms import packaging_profile, packaging_profiles

STOPWORDS = {
    "a", "about", "actually", "after", "again", "all", "already", "also", "am", "an", "and", "any",
    "are", "as", "at", "back", "be", "because", "become", "becomes", "been", "before", "behind",
    "being", "best", "better", "biggest", "but", "by", "can", "could", "did", "different", "do",
    "does", "doing", "done", "down", "easy", "even", "ever", "every", "expensive", "first", "for",
    "from", "full", "get", "good", "got", "great", "had", "hard", "has", "have", "he", "her", "here",
    "him", "his", "how", "i", "if", "important", "in", "into", "is", "it", "its", "just", "know",
    "last", "least", "less", "like", "little", "many", "maybe", "me", "more", "most", "much", "must",
    "my", "never", "next", "no", "not", "now", "obvious", "of", "often", "on", "one", "only", "or",
    "other", "our", "out", "over", "own", "perhaps", "quite", "rather", "real", "really", "right",
    "said", "say", "see", "she", "should", "simple", "since", "single", "so", "some", "something",
    "still", "such", "sure", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "thing", "think", "this", "those", "though", "through", "to", "today", "together", "too", "true",
    "under", "until", "up", "upon", "us", "very", "was", "way", "we", "well", "were", "what", "when",
    "where", "whether", "which", "whole", "who", "why", "will", "with", "within", "without", "would",
    "yet", "you", "your",
}

BROAD_HASHTAGS = {
    "tiktok": [("fyp", "Broad TikTok discovery tag; no popularity claim is made.")],
    "youtube_shorts": [("shorts", "YouTube's own Shorts format tag, not a trend claim.")],
    "instagram_reels": [("reels", "Instagram's own Reels format tag, not a trend claim.")],
    "cross_platform": [],
}

GENRE_LABELS = {
    "podcast_interview": "podcast",
    "comedy": "comedy",
    "storytime": "storytelling",
    "education": "education",
    "expert_advice": "expert advice",
    "debate": "debate",
    "emotional_inspirational": "inspiration",
    "product_demo": "product demo",
    "livestream_highlights": "livestream",
    "slide_webinar": "webinar",
    "auto": "",
}

CALL_TO_ACTIONS = {
    "follow": "Follow for more clips like this.",
    "comment": "Tell us what you think in the comments.",
    "link": "Find the full conversation through the link in the profile.",
}


@dataclass(frozen=True)
class PackagingContext:
    transcript: dict
    candidate: dict
    platform: str = "cross_platform"
    settings: dict = field(default_factory=dict)
    genre: str = "auto"
    strategy: str = "chronological"
    topic: str = ""
    subtopics: list[str] = field(default_factory=list)
    banned_terms: list[str] = field(default_factory=list)
    now: datetime | None = None


@dataclass(frozen=True)
class ClipSentence:
    text: str
    start_ms: int
    end_ms: int
    sentence_id: int


@dataclass(frozen=True)
class ClipText:
    sentences: list[ClipSentence]
    full_text: str
    source_start_ms: int
    source_end_ms: int
    timeline_applied: bool
    warnings: list[str]
    sentence_ids: list[int]


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _int_value(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _word_rows(transcript: dict, spans: list[tuple[int, int]]) -> list[dict]:
    rows: list[dict] = []
    sentence_id = 0
    segments = transcript.get("segments") or []
    # Iterate the approved editorial spans in order. A non-linear plan may put a
    # later source moment first, and the packaging text must follow final playback,
    # not the transcript's original chronological order.
    for span_start, span_end in spans:
        for segment in segments:
            words = segment.get("words") or []
            if not words:
                start = int(segment.get("start_ms") or 0)
                end = int(segment.get("end_ms") or 0)
                if start < span_end and end > span_start:
                    rows.append(
                        {
                            "word": segment.get("text") or "",
                            "start_ms": start,
                            "end_ms": end,
                            "sentence_id": sentence_id,
                            "segment": True,
                        }
                    )
                    sentence_id += 1
                continue
            for word in words:
                try:
                    start = int(word.get("start_ms") or 0)
                    end = int(word.get("end_ms") or start)
                except (TypeError, ValueError):
                    continue
                midpoint = (start + end) // 2
                if span_start <= midpoint < span_end:
                    rows.append(
                        {
                            "word": str(word.get("word") or ""),
                            "start_ms": start,
                            "end_ms": end,
                            "sentence_id": sentence_id,
                            "segment": False,
                        }
                    )
    return rows


def _sentences_from_rows(rows: list[dict]) -> list[ClipSentence]:
    if not rows:
        return []
    sentences: list[ClipSentence] = []
    buffer: list[dict] = []
    sentence_id = -1

    def flush() -> None:
        nonlocal buffer, sentence_id
        if not buffer:
            return
        text = _clean(" ".join(str(row["word"]) for row in buffer))
        if text:
            sentence_id += 1
            sentences.append(
                ClipSentence(
                    text=text,
                    start_ms=int(buffer[0]["start_ms"]),
                    end_ms=int(buffer[-1]["end_ms"]),
                    sentence_id=sentence_id,
                )
            )
        buffer = []

    for row in rows:
        buffer.append(row)
        token = str(row["word"]).strip()
        ended = token.endswith((".", "!", "?"))
        long_enough = len(buffer) >= 28
        if ended or long_enough:
            flush()
    flush()
    return sentences


def _spans_from_candidate(candidate: dict) -> tuple[list[tuple[int, int]], bool, list[str]]:
    editorial = candidate.get("editorial") or {}
    decision = editorial.get("editorial") or {}
    timeline = editorial.get("timeline") or decision.get("timeline") or {}
    warnings: list[str] = []
    applied = bool(decision.get("applied"))
    entries = timeline.get("entries") or []
    if applied and timeline.get("valid") and entries:
        spans = []
        for entry in entries:
            start = int(entry.get("source_start_ms") or 0)
            end = int(entry.get("source_end_ms") or 0)
            if end > start:
                spans.append((start, end))
        if spans:
            return spans, True, warnings
        warnings.append("The applied editorial timeline had no usable spans; packaging fell back to the clip window.")
    elif applied and entries:
        warnings.append("The applied editorial timeline is marked invalid; packaging used the approved clip window.")
    elif entries:
        warnings.append("A non-linear plan exists but is not applied; packaging used the approved clip window.")
    return [(int(candidate.get("start_ms") or 0), int(candidate.get("end_ms") or 0))], False, warnings


def clip_text(transcript: dict, candidate: dict) -> ClipText:
    spans, applied, warnings = _spans_from_candidate(candidate)
    rows = _word_rows(transcript, spans)
    sentences = _sentences_from_rows(rows)
    if not sentences:
        text = _clean(candidate.get("excerpt") or candidate.get("summary") or "")
        if text:
            sentences = [
                ClipSentence(
                    text=text,
                    start_ms=int(candidate.get("start_ms") or 0),
                    end_ms=int(candidate.get("end_ms") or 0),
                    sentence_id=0,
                )
            ]
            warnings.append("No word timings were available; packaging used the stored clip excerpt.")
    full_text = _clean(" ".join(sentence.text for sentence in sentences))
    return ClipText(
        sentences=sentences,
        full_text=full_text,
        source_start_ms=spans[0][0] if spans else 0,
        source_end_ms=max((span[1] for span in spans), default=0),
        timeline_applied=applied,
        warnings=warnings,
        sentence_ids=[sentence.sentence_id for sentence in sentences],
    )


def _measure(value: str, unit: str = "characters") -> int:
    """Count text the way the target platform does.

    TikTok counts caption length in UTF-16 code units, so an emoji costs two.
    Python's ``len`` counts code points and would understate that. Other
    profiles in the current configuration use characters.
    """
    if "utf-16" in unit.lower():
        return len((value or "").encode("utf-16-le")) // 2
    return len(value or "")


def _trim(text: str, limit: int, unit: str = "characters") -> str:
    value = _clean(text)
    if limit <= 0 or _measure(value, unit) <= limit:
        return value
    cut = value[:limit]
    while cut and _measure(cut, unit) > limit:
        cut = cut[:-1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    cut = cut.rstrip(" ,;:!?-")
    if cut:
        return cut
    fallback = value[:limit]
    while fallback and _measure(fallback, unit) > limit:
        fallback = fallback[:-1]
    return fallback


def _chars(value: str) -> int:
    return len(value or "")


def _sentence_pool(text: ClipText, hook_text: str) -> list[ClipSentence]:
    pool = list(text.sentences)
    hook = _clean(hook_text)
    if hook:
        matching = [sentence for sentence in pool if hook[:40].lower() in sentence.text.lower()]
        if matching:
            pool = matching + [sentence for sentence in pool if sentence not in matching]
    return pool


def _title_variants(text: ClipText, hook_text: str, profile: dict[str, object]) -> list[TitleVariant]:
    limit = _int_value(profile.get("title_max"), 100)
    recommended = _int_value(profile.get("title_recommended"), 60)
    pool = _sentence_pool(text, hook_text)
    variants: list[TitleVariant] = []
    seen: set[str] = set()

    def add(candidate: str, style: str, reason: str, basis: str = "transcript") -> None:
        value = _trim(candidate, recommended)
        if len(value) < 8:
            value = _trim(candidate, limit)
        key = value.lower()
        if not value or key in seen:
            return
        seen.add(key)
        variants.append(
            TitleVariant(
                text=value,
                style=style,  # type: ignore[arg-type]
                reason=reason,
                chars=_chars(value),
                limit=limit,
                within_limit=_chars(value) <= limit,
                basis=basis,  # type: ignore[arg-type]
            )
        )

    if hook_text:
        exact = _clean(hook_text)
        reason = (
            "Uses the approved hook text exactly."
            if _chars(exact) <= recommended
            else "Starts from the approved hook text; trimmed to the platform's recommended title length."
        )
        add(hook_text, "quote", reason, "hook")
    for sentence in pool[:4]:
        add(sentence.text, "direct", "Uses a sentence from the final clip in order.")
    questions = [sentence for sentence in pool if sentence.text.rstrip().endswith("?")]
    if questions:
        add(questions[0].text, "question", "Uses a question that actually appears in the clip.")
    result_like = [sentence for sentence in pool if re.search(r"\b\d", sentence.text)]
    if result_like:
        add(result_like[0].text, "result", "Uses a sentence containing a real number from the clip.")
    first_clause = re.split(r"[.,;:!?-]", pool[0].text, maxsplit=1)[0] if pool else ""
    if first_clause:
        add(first_clause, "curiosity", "Uses the opening clause without adding a claim.")
    return variants[:5]


def _topic(context: PackagingContext, text: ClipText) -> str:
    explicit = _clean(context.topic) or _clean(str(context.settings.get("niche") or ""))
    if explicit:
        return explicit[:80]
    genre = GENRE_LABELS.get(context.genre, "")
    if genre:
        return genre
    terms = _top_terms(text.full_text, 1)
    return terms[0] if terms else "clip"


def _top_terms(value: str, count: int) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", value or "")
    counts: dict[str, int] = {}
    for word in words:
        key = word.lower().strip("'-")
        if key in STOPWORDS or len(key) < 3:
            continue
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:count]]


def _hashtag(value: str) -> str:
    cleaned = "".join(character for character in value if character.isalnum())
    return f"#{cleaned}" if cleaned else ""


def _hashtags(context: PackagingContext, text: ClipText, topic: str) -> list[HashtagSuggestion]:
    profile = packaging_profile(context.platform)
    style = str(context.settings.get("hashtag_style") or "balanced")
    count = {
        "minimal": 3,
        "balanced": 5,
        "descriptive": 8,
    }.get(style, 5)
    raw_requested = context.settings.get("hashtag_count")
    requested = _int_value(raw_requested, count) if raw_requested is not None else count
    target = max(0, min(_int_value(profile.get("hashtag_max"), 30), requested))
    if target == 0:
        return []

    suggestions: list[HashtagSuggestion] = []
    seen: set[str] = set()

    def add(value: str, category: str, reason: str, provenance: str, relevance: float) -> None:
        tag = _hashtag(value)
        if not tag:
            return
        key = tag.lower()
        # Treat simple plurals as duplicates so "#customer" and "#customers"
        # never both consume one of a small hashtag budget.
        dedupe_key = key[:-1] if len(key) > 5 and key.endswith("s") else key
        if dedupe_key in seen:
            return
        if not 3 <= len(tag) <= 42:
            return
        seen.add(dedupe_key)
        suggestions.append(
            HashtagSuggestion(
                tag=tag,
                category=category,  # type: ignore[arg-type]
                reason=reason,
                provenance=provenance,  # type: ignore[arg-type]
                relevance=max(0.0, min(1.0, relevance)),
            )
        )

    add(topic, "niche", f"Named topic/niche supplied for this project: {topic}.", "profile", 0.95)
    for subtopic in context.subtopics[:3]:
        add(subtopic, "topic", f"Named subtopic supplied for this project: {subtopic}.", "profile", 0.85)
    for term in _top_terms(text.full_text, 6):
        add(term, "topic", "Recurring word in the final clip transcript.", "transcript", 0.7)
    genre = GENRE_LABELS.get(context.genre, "")
    if genre:
        add(genre, "broad", "Genre selected for this clip; not a trend claim.", "genre", 0.55)
    for term in context.settings.get("brand_terms") or []:
        add(str(term), "niche", "Brand term from the client profile.", "profile", 0.8)
    for name, reason in BROAD_HASHTAGS.get(context.platform, []):
        add(name, "broad", reason, "platform", 0.3)

    suggestions.sort(key=lambda item: (-item.relevance, item.tag.lower()))
    return suggestions[:target]


def _keywords(context: PackagingContext, text: ClipText, topic: str, titles: list[TitleVariant]) -> list[SearchKeyword]:
    profile = packaging_profile(context.platform)
    limit = _int_value(profile.get("keyword_max"), 10)
    keywords: list[SearchKeyword] = []
    seen: set[str] = set()

    def add(phrase: str, reason: str) -> None:
        value = _clean(phrase).strip("#")
        if len(value) < 3 or len(value) > 80:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        keywords.append(
            SearchKeyword(
                phrase=value,
                reason=reason,
                source="ai_generated",
                verified=False,
            )
        )

    genre = GENRE_LABELS.get(context.genre, "")
    add(f"{topic} {genre}".strip(), "Natural-language phrase combining the project topic and genre.")
    if titles:
        add(titles[0].text.rstrip("."), "Phrase taken from the strongest grounded title.")
    for sentence in text.sentences[:3]:
        phrase = _trim(sentence.text, 90).rstrip(".")
        if len(phrase.split()) >= 3:
            add(phrase, "Phrase spoken in the final clip.")
    return keywords[:limit]


def _cta(context: PackagingContext) -> str | None:
    preference = str(context.settings.get("cta_preference") or "auto")
    custom = _clean(str(context.settings.get("cta_text") or ""))
    if preference == "none":
        return None
    if preference == "custom":
        return custom[:160] or None
    if preference in CALL_TO_ACTIONS:
        return CALL_TO_ACTIONS[preference]
    if context.genre in {"education", "expert_advice", "slide_webinar"}:
        return CALL_TO_ACTIONS["follow"]
    if context.genre in {"debate", "comedy"}:
        return CALL_TO_ACTIONS["comment"]
    return CALL_TO_ACTIONS["follow"]


def _caption(
    platform: str,
    text: ClipText,
    hook_text: str,
    topic: str,
    hashtags: list[HashtagSuggestion],
    cta: str | None,
) -> PlatformCaption:
    profile = packaging_profile(platform)
    limit = _int_value(profile.get("caption_max"), 2_200)
    unit = str(profile.get("caption_hard_limit_unit") or "characters")
    opening = _trim(hook_text or (text.sentences[0].text if text.sentences else text.full_text), 120)
    support = _trim(text.sentences[1].text if len(text.sentences) > 1 else "", 160)
    basis = " ".join(part for part in (opening, support) if part).strip()
    if not basis:
        basis = _trim(text.full_text or topic, 200)

    tag_limit = _int_value(profile.get("hashtag_max"), 30)
    tags = [item.tag for item in hashtags][:tag_limit]
    pieces = [basis]
    if cta:
        pieces.append(cta)
    candidate = " ".join(pieces)
    if tags:
        with_tags = f"{candidate} {' '.join(tags)}".strip()
        if _measure(with_tags, unit) <= limit:
            candidate = with_tags
        else:
            # Keep whole hashtags rather than cutting a tag in half.
            kept: list[str] = []
            for tag in tags:
                attempt = f"{candidate} {' '.join(kept + [tag])}".strip()
                if _measure(attempt, unit) <= limit:
                    kept.append(tag)
            if kept:
                candidate = f"{candidate} {' '.join(kept)}".strip()
    if _measure(candidate, unit) > limit:
        candidate = _trim(candidate, limit, unit)
    count = _measure(candidate, unit)
    notes = [
        f"{count} of {limit} {unit}.",
        "Hashtags and keywords are suggestions, not evidence of current popularity.",
    ]
    return PlatformCaption(
        platform=platform,
        label=str(profile.get("label") or platform),
        text=candidate,
        char_count=count,
        limit=limit,
        within_limit=count <= limit,
        notes=notes,
    )


def _description(text: ClipText, hashtags: list[HashtagSuggestion]) -> DescriptionVariants:
    profile = packaging_profile("cross_platform")
    short_limit = _int_value(profile.get("description_short_max"), 150)
    long_limit = _int_value(profile.get("description_long_max"), 2_200)
    sentences = [sentence.text for sentence in text.sentences]
    short = _trim(sentences[0] if sentences else text.full_text, short_limit)
    if len(sentences) > 1 and len(f"{short} {sentences[1]}") <= short_limit:
        short = f"{short} {sentences[1]}"
    long_parts: list[str] = []
    for sentence in sentences:
        attempt = " ".join(long_parts + [sentence])
        if len(attempt) > long_limit:
            break
        long_parts.append(sentence)
    if not long_parts:
        long_parts = [_trim(text.full_text, long_limit)]
    long_text = " ".join(long_parts)
    tags = [item.tag for item in hashtags]
    if tags:
        with_tags = f"{long_text} {' '.join(tags)}".strip()
        if len(with_tags) <= long_limit:
            long_text = with_tags
    return DescriptionVariants(
        short=short,
        long=long_text,
        short_limit=short_limit,
        long_limit=long_limit,
        within_limit=len(short) <= short_limit and len(long_text) <= long_limit,
    )


def _thumbnail(text: ClipText, hook_text: str, platform: str) -> ThumbnailText | None:
    profile = packaging_profile(platform)
    limit = _int_value(profile.get("thumbnail_text_max"), 40)
    value = _trim(hook_text or (text.sentences[0].text if text.sentences else text.full_text), limit)
    if not value:
        return None
    return ThumbnailText(
        text=value,
        limit=limit,
        within_limit=len(value) <= limit,
        reason="Short verbatim phrase from the clip; no claim was added.",
    )


def _numbers_in(value: str) -> set[str]:
    return set(re.findall(r"\b\d[\d.,]*\b", value or ""))


def _validate(
    metadata: ClipMetadata,
    *,
    context: PackagingContext,
    text: ClipText,
    topic: str,
) -> MetadataValidation:
    profile = packaging_profile(context.platform)
    issues: list[str] = []
    warnings: list[str] = list(text.warnings)
    if not metadata.titles:
        issues.append("No grounded title could be produced from the clip transcript.")
    for title in metadata.titles:
        if not title.within_limit:
            issues.append(f"Title exceeds the {profile.get('label')} limit of {title.limit} characters.")
    if not metadata.caption.within_limit:
        issues.append(
            f"Caption exceeds the {metadata.caption.limit} "
            f"{profile.get('caption_hard_limit_unit', 'character')} limit."
        )
    if len(metadata.hashtags) > _int_value(profile.get("hashtag_max"), 30):
        issues.append("Too many hashtags for the selected platform.")
    if len(metadata.keywords) > _int_value(profile.get("keyword_max"), 10):
        warnings.append("Keyword list was trimmed to the platform profile limit.")

    source_lower = " ".join(
        [
            text.full_text,
            topic,
            *(str(value) for value in context.subtopics),
            str(context.settings.get("niche") or ""),
            *(str(value) for value in context.settings.get("brand_terms") or []),
        ]
    ).lower()
    generated_text = " ".join(
        [title.text for title in metadata.titles]
        + [metadata.descriptions.short, metadata.descriptions.long, metadata.caption.text]
        + [caption.text for caption in metadata.platform_captions]
        + [item.tag for item in metadata.hashtags]
        + [keyword.phrase for keyword in metadata.keywords]
        + [metadata.cta or ""]
        + [metadata.thumbnail_text.text if metadata.thumbnail_text else ""]
    )
    for number in _numbers_in(generated_text):
        if number not in source_lower:
            issues.append(f"Number '{number}' in generated metadata does not appear in the clip transcript or supplied topic.")

    restricted = [
        *(str(value) for value in context.settings.get("restricted_words") or []),
        *(str(value) for value in context.settings.get("restricted_topics") or []),
        *(str(value) for value in context.settings.get("restricted_claims") or []),
        *(str(value) for value in context.banned_terms or []),
    ]
    for term in restricted:
        if term and term.lower() in generated_text.lower():
            issues.append(f"Restricted term or claim appears in metadata: {term}")

    forbidden_claims = ("guaranteed", "100%", "must see", "will change your life", "viral")
    for claim in forbidden_claims:
        if claim in generated_text.lower():
            if claim in source_lower:
                warnings.append(f"Promotional phrase '{claim}' appears in the source; verify it is quoted honestly.")
            else:
                issues.append(f"Unverifiable promotional language appears in metadata: {claim}")

    if not topic:
        warnings.append("No topic or niche was supplied; hashtags use transcript terms only.")
    warnings.append("Search keywords are AI-generated and are not verified platform search data.")
    return MetadataValidation(
        ok=not issues,
        issues=issues,
        warnings=list(dict.fromkeys(warnings)),
        platform=context.platform,
    )


def generate_metadata(context: PackagingContext) -> ClipMetadata:
    platform = context.platform if context.platform in packaging_profiles() else "cross_platform"
    context = PackagingContext(
        transcript=context.transcript,
        candidate=context.candidate,
        platform=platform,
        settings=context.settings,
        genre=context.genre,
        strategy=context.strategy,
        topic=context.topic,
        subtopics=context.subtopics,
        banned_terms=context.banned_terms,
        now=context.now,
    )
    text = clip_text(context.transcript, context.candidate)
    hook = _clean((context.candidate.get("hook_detail") or {}).get("text") or context.candidate.get("hook") or "")
    if hook and hook.lower() not in text.full_text.lower():
        hook = ""
    topic = _topic(context, text)
    titles = _title_variants(text, hook, packaging_profile(platform))
    hashtags = _hashtags(context, text, topic)
    keywords = _keywords(context, text, topic, titles) if context.settings.get("include_keywords", True) else []
    cta = _cta(context)
    descriptions = _description(text, hashtags)
    captions = [
        _caption(name, text, hook, topic, hashtags, cta)
        for name in packaging_profiles()
    ]
    caption = next((item for item in captions if item.platform == platform), captions[0])
    thumbnail = _thumbnail(text, hook, platform) if context.settings.get("thumbnail_text", True) else None
    now = (context.now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    metadata = ClipMetadata(
        titles=titles,
        descriptions=descriptions,
        caption=caption,
        platform_captions=captions,
        hashtags=hashtags,
        keywords=keywords,
        cta=cta,
        thumbnail_text=thumbnail,
        provenance=PackagingProvenance(
            generated_at=now.isoformat(),
            method="deterministic",
            provider="local",
            transcript_id=context.transcript.get("id"),
            source_start_ms=text.source_start_ms,
            source_end_ms=text.source_end_ms,
            timeline_applied=text.timeline_applied,
            sentence_ids=text.sentence_ids,
            grounding=[
                "Titles, descriptions and captions use transcript sentences from the final clip.",
                "Hashtags use the project topic, profile terms, genre and transcript terms.",
                "No search volume, ranking or growth figure is generated locally.",
            ],
            warnings=list(text.warnings),
        ),
    )
    metadata.validation = _validate(metadata, context=context, text=text, topic=topic)
    return metadata
