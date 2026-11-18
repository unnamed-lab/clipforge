"""Editorial packaging director: candidates, scoring and descriptions.

All generated text is derived from the evidence in a :class:`ContentBrief`.
Accuracy is checked before ranking. Scores are editorial fit only; they are not
predictions of views, click-through rate or virality.
"""

from __future__ import annotations

import re

from clipforge.packaging.brief import _content_tokens, _proper_nouns, clean
from clipforge.packaging.models import (
    ContentBrief,
    DescriptionVariants,
    KeywordResearchResult,
    TitleScores,
    TitleVariant,
)
from clipforge.packaging.skill import ACCURACY_GATE, TITLE_ANGLES, title_weights
from clipforge.platforms import packaging_profile

GENERIC_PHRASES = (
    "you won't believe",
    "you wont believe",
    "must watch",
    "must see",
    "game changer",
    "life changing",
    "secret revealed",
    "nobody talks about this",
    "this changes everything",
)
FORBIDDEN_CLAIMS = ("guaranteed", "100%", "viral", "will change your life", "must watch")
SCAFFOLD_WORDS = {
    "the", "a", "an", "how", "to", "why", "what", "this", "that", "clip", "lesson", "fix",
    "about", "and", "is", "are", "was", "were", "in", "on", "for", "of", "from", "with",
    "your", "you", "we", "i", "it", "that's", "part", "most", "when", "who", "can", "do",
}


def _trim(text: str, limit: int) -> str:
    value = clean(text)
    if limit <= 0 or len(value) <= limit:
        return value
    cut = value[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:!?-")


def _sentence_case_words(value: str) -> str:
    text = clean(value)
    if not text:
        return text
    return text[0].upper() + text[1:]


def _strip_end(value: str) -> str:
    return clean(value).rstrip(" .,;:!?")


def _strip_lead(value: str) -> str:
    text = clean(value)
    for prefix in ("But ", "And ", "So ", "Then ", "Well ", "Okay, ", "You know, "):
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


REFERENTIAL_OPENERS = (
    "this ",
    "that ",
    "these ",
    "those ",
    "it ",
    "they ",
    "there ",
    "which ",
)


def _starts_with_reference(value: str) -> bool:
    """True when a sentence only makes sense after another sentence."""
    return clean(value).lower().startswith(REFERENTIAL_OPENERS)


def _lower_first(value: str) -> str:
    text = clean(value)
    if not text:
        return text
    if text[:2].isupper() and text[1].isupper():
        return text
    return text[0].lower() + text[1:]


def _clause(value: str, *, max_words: int = 14) -> str:
    """Return the first natural clause, never a half word."""
    text = _strip_end(value)
    for separator in (". ", "? ", "! ", "; ", ": ", ", ", " and ", " but ", " because ", " so "):
        if separator in text:
            candidate = text.split(separator, 1)[0].strip()
            if len(candidate.split()) >= 4:
                text = candidate
                break
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,;:!?-")
    return clean(text)


def _fit(value: str, limit: int, recommended: int) -> str:
    """Complete sentences keep their payoff up to the hard limit."""
    text = clean(value)
    complete = text.endswith((".", "!", "?", "…"))
    return _trim(text, limit if complete else recommended)


def _dedupe_key(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"\b\d[\d.,]*\b", value or ""))


def _clip_terms(clip_sentences: list[dict]) -> set[str]:
    terms: set[str] = set()
    for sentence in clip_sentences:
        terms |= _content_tokens(str(sentence.get("text") or ""))
    return terms


def accuracy_gate(
    text: str,
    *,
    brief: ContentBrief,
    clip_full_text: str,
    clip_sentences: list[dict],
    settings: dict | None = None,
    require_clip_overlap: bool = True,
) -> list[str]:
    """Return reasons the candidate must be rejected. Empty means it passed."""
    settings = settings or {}
    reasons: list[str] = []
    lower = text.lower()
    allowed_source = " ".join(
        [
            clip_full_text,
            *(str(value) for value in brief.entities),
            *(str(value) for value in settings.get("brand_terms") or []),
        ]
    ).lower()
    for number in _numbers(text):
        if number not in allowed_source:
            reasons.append(f"Number '{number}' is not in the clip evidence.")
    for token in _proper_nouns(text):
        if token.lower() not in allowed_source and token.lower() not in SCAFFOLD_WORDS:
            reasons.append(f"Name '{token}' is not in the clip evidence.")
    for claim in FORBIDDEN_CLAIMS:
        if claim in lower and claim not in clip_full_text.lower():
            reasons.append(f"Unsupported promotional claim '{claim}'.")
    for phrase in GENERIC_PHRASES:
        if phrase in lower:
            reasons.append(f"Generic clickbait phrase: '{phrase}'.")
    if not text.strip():
        reasons.append("The title is empty.")
    if re.match(r"^(this|that|these|those|it|they|there)\b", lower):
        reasons.append(
            "The title opens with a reference ('this', 'that', 'it') that has no antecedent in a title."
        )
    if len(text) > int(packaging_profile(settings.get("platform") if isinstance(settings.get("platform"), str) else "").get("title_max") or 100):
        reasons.append("The title exceeds the platform limit.")
    if _numbers(text) and not _numbers(clip_full_text):
        reasons.append("The title introduces a number the clip does not contain.")
    clip_terms = _clip_terms(clip_sentences)
    title_terms = _content_tokens(text) - SCAFFOLD_WORDS
    overlap = len(title_terms & clip_terms)
    if require_clip_overlap and title_terms and overlap == 0:
        reasons.append("The title does not use any concept from the selected clip.")
    # Context leakage: a title built mainly from a wider-context summary would
    # imply the clip contains material it does not.
    context_only = " ".join(
        item.text for item in brief.evidence if not item.in_clip
    )
    if context_only:
        context_terms = _content_tokens(context_only) - SCAFFOLD_WORDS
        if context_terms and title_terms:
            context_overlap = len(title_terms & context_terms) / max(1, len(title_terms))
            clip_overlap = len(title_terms & clip_terms) / max(1, len(title_terms))
            if context_overlap >= 0.75 and context_overlap > clip_overlap + 0.15:
                reasons.append("The title relies on material from outside the selected clip.")
    return reasons


def _specificity(text: str) -> float:
    score = 0.35
    if _numbers(text):
        score += 0.25
    score += min(0.2, 0.07 * len(_proper_nouns(text)))
    words = [word for word in _content_tokens(text) if len(word) > 3]
    score += min(0.2, 0.04 * len(words))
    if len(set(words)) == len(words) and len(words) >= 4:
        score += 0.05
    return max(0.0, min(1.0, score))


def _audience_relevance(text: str, brief: ContentBrief, settings: dict) -> float:
    target = _content_tokens(
        " ".join(
            [
                str(settings.get("audience") or ""),
                brief.audience_problem,
                brief.clip_topic,
                " ".join(brief.search_concepts[:4]),
            ]
        )
    )
    if not target:
        return 0.5
    terms = _content_tokens(text)
    return max(0.0, min(1.0, 0.25 + 0.75 * (len(terms & target) / max(1, len(target)))))


def _curiosity(text: str) -> float:
    value = text.lower()
    score = 0.3
    if value.strip().endswith("?"):
        score += 0.2
    if re.search(r"\b(why|how|what|the mistake|the lesson|the truth|turns out|actually)\b", value):
        score += 0.25
    if re.search(r"\b(but|instead|never|stop|wrong)\b", value):
        score += 0.15
    if not _numbers(value) and len(_content_tokens(value)) >= 4:
        score += 0.1
    if any(phrase in value for phrase in GENERIC_PHRASES):
        score -= 0.5
    return max(0.0, min(1.0, score))


def _search_relevance(text: str, brief: ContentBrief, research: KeywordResearchResult | None) -> float:
    concepts = set(brief.search_concepts)
    if research:
        concepts |= set(research.suggested_terms)
    if not concepts:
        return 0.2
    terms = _content_tokens(text)
    target = {token for concept in concepts for token in _content_tokens(concept)}
    if not target:
        return 0.2
    return max(0.0, min(1.0, len(terms & target) / max(1, min(len(target), 5))))


def _readability(text: str, recommended: int) -> float:
    length = len(text)
    words = len(text.split())
    score = 0.5
    if 28 <= length <= max(40, recommended):
        score += 0.25
    elif length < 18 or length > recommended:
        score -= 0.2
    if 4 <= words <= 13:
        score += 0.15
    if text.upper() == text and len(text) > 6:
        score -= 0.3
    if len(re.findall(r"\b(\w+)\s+\1\b", text, re.I)):
        score -= 0.2
    return max(0.0, min(1.0, score))


def score_titles(
    candidates: list[dict],
    *,
    brief: ContentBrief,
    clip_full_text: str,
    clip_sentences: list[dict],
    platform: str,
    settings: dict,
    research: KeywordResearchResult | None = None,
    weights: dict[str, float] | None = None,
    require_clip_overlap: bool = True,
) -> list[TitleVariant]:
    profile = packaging_profile(platform)
    title_max = int(profile.get("title_max") or 100)
    recommended = int(profile.get("title_recommended") or 60)
    resolved_weights = weights or title_weights()
    scored: list[TitleVariant] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = clean(str(candidate.get("text") or ""))
        if not text:
            continue
        key = _dedupe_key(text)
        if key in seen:
            continue
        seen.add(key)
        rejected = accuracy_gate(
            text,
            brief=brief,
            clip_full_text=clip_full_text,
            clip_sentences=clip_sentences,
            settings=settings,
            require_clip_overlap=require_clip_overlap,
        )
        within = len(text) <= title_max
        if not within:
            rejected.append(f"The title exceeds the {profile.get('label')} limit of {title_max} characters.")
        scores = TitleScores(
            accuracy=0.0 if rejected else 1.0,
            specificity=_specificity(text),
            audience_relevance=_audience_relevance(text, brief, settings),
            curiosity=_curiosity(text),
            search_relevance=_search_relevance(text, brief, research),
            readability=_readability(text, recommended),
        )
        # Accuracy is a gate. The weighted score is still recorded for review,
        # but a rejected candidate can never rank above an accepted one.
        scores.weighted = round(
            sum(getattr(scores, key) * weight for key, weight in resolved_weights.items()), 3
        )
        if rejected:
            scores.accuracy = 0.0
        scored.append(
            TitleVariant(
                text=text,
                style=_legacy_style(str(candidate.get("angle") or "descriptive")),
                reason=clean(str(candidate.get("rationale") or candidate.get("reason") or ""))[:240],
                chars=len(text),
                limit=title_max,
                within_limit=within,
                basis="transcript" if candidate.get("in_clip", True) else "topic",
                angle=str(candidate.get("angle") or "descriptive"),  # type: ignore[arg-type]
                evidence_ids=[str(value) for value in candidate.get("evidence_ids") or []],
                limitations=[str(value) for value in candidate.get("limitations") or []],
                scores=scores,
                accepted=not rejected and scores.accuracy >= ACCURACY_GATE,
                rejection_reason="; ".join(rejected),
            )
        )
    return rank_titles(scored)


def rank_titles(candidates: list[TitleVariant]) -> list[TitleVariant]:
    return sorted(
        candidates,
        key=lambda item: (not item.accepted, -(item.scores.weighted if item.scores else 0.0), item.chars),
    )


def _legacy_style(angle: str) -> str:
    return {
        "question": "question",
        "benefit": "result",
        "problem_solution": "how_to",
        "story": "quote",
        "insight": "curiosity",
    }.get(angle, "direct")


def _evidence_id_for(brief: ContentBrief, text: str) -> list[str]:
    target = _strip_end(text).lower()
    if not target:
        return []
    exact = [item.id for item in brief.evidence if clean(item.text).lower() == target]
    if exact:
        return exact[:2]
    prefix = [
        item.id
        for item in brief.evidence
        if item.in_clip and clean(item.text).lower().startswith(target[: max(24, len(target) // 2)])
    ]
    return prefix[:2]


def title_candidates(
    brief: ContentBrief,
    *,
    clip_sentences: list[dict],
    platform: str,
    settings: dict,
    research: KeywordResearchResult | None = None,
    requested_angle: str | None = None,
) -> list[TitleVariant]:
    """Build meaningfully different candidates from distinct evidence."""
    profile = packaging_profile(platform)
    title_max = int(profile.get("title_max") or 100)
    recommended = int(profile.get("title_recommended") or 60)
    settings = {**settings, "platform": platform}
    raw: list[dict] = []

    def add(text: str, angle: str, rationale: str, evidence: str | None = None, limitations: list[str] | None = None) -> None:
        value = _fit(text, title_max, recommended)
        if len(value) < 8:
            return
        raw.append(
            {
                "text": value,
                "angle": angle,
                "rationale": rationale,
                "evidence_ids": _evidence_id_for(brief, evidence or ""),
                "limitations": limitations or [],
                "in_clip": True,
            }
        )

    claim = brief.central_claim
    insight = brief.key_insight
    takeaway = brief.practical_takeaway
    problem = brief.audience_problem
    hook = brief.stronger_hook
    claim_clause = _strip_lead(_clause(claim))
    insight_clause = _strip_lead(_clause(insight))
    takeaway_clause = _strip_lead(_clause(takeaway))

    if claim:
        add(_sentence_case_words(_strip_end(claim_clause)), "descriptive", "States the clip's central claim directly.", claim)
    if insight and _dedupe_key(insight_clause) != _dedupe_key(claim_clause):
        add(_sentence_case_words(_strip_end(insight_clause)), "insight", "Leads with the most specific supporting insight.", insight)
    if problem and _dedupe_key(problem) != _dedupe_key(claim):
        add(f"The real problem: {_lower_first(_strip_end(_strip_lead(_clause(problem, max_words=10))))}", "problem_solution",
            "Names the audience problem the clip addresses.", problem)
    if takeaway:
        first = re.sub(r"[^A-Za-z]", "", takeaway.strip().split(" ")[0]).lower()
        if first in {
            "talk", "start", "write", "use", "try", "hire", "build", "measure", "ask", "check",
            "focus", "stop", "avoid", "pick", "choose", "test", "read", "listen", "watch", "learn",
            "make", "take", "keep", "set", "plan", "review", "track", "ship", "practice", "practise",
        }:
            add(f"How to {takeaway_clause[0].lower() + takeaway_clause[1:]}", "problem_solution",
                "Turns the clip's own practical instruction into a how-to.", takeaway)
        else:
            add(_strip_end(takeaway_clause), "benefit", "Leads with the clip's practical takeaway.", takeaway)
    question = next((item for item in clip_sentences if str(item.get("text") or "").strip().endswith("?")), None)
    if question:
        add(str(question["text"]), "question", "Uses a question that actually appears in the clip.", str(question["text"]))
    elif claim and re.search(r"\b(is|are|was|were)\b", claim):
        add(
            f"Why {_strip_end(claim)[0].lower() + _strip_end(claim)[1:]}",
            "question",
            "Frames the clip's actual claim as a question without adding a new claim.",
            claim,
            ["Question framing must not imply a conclusion the clip does not make."],
        )
    if brief.example_evidence:
        example = _strip_lead(_clause(brief.example_evidence[0], max_words=12))
        if len(example.split()) >= 4:
            add(_sentence_case_words(example), "story", "Leads with a concrete example from the clip.", brief.example_evidence[0])
    if brief.caveats:
        caveat = _strip_lead(_clause(brief.caveats[0], max_words=12))
        if len(caveat.split()) >= 4:
            add(_sentence_case_words(caveat), "contrarian", "Uses a qualification the speaker actually makes.", brief.caveats[0],
                ["Only use this framing if the qualifier is central to the clip."])
    if brief.clip_topic:
        topic = _sentence_case_words(brief.clip_topic)
        base = _clause(claim or insight or hook, max_words=9)
        if base:
            add(f"{topic}: {_lower_first(_strip_end(base))}", "search", "Pairs the clip's topic phrase with its main point.", claim or insight or hook)
    if hook and _dedupe_key(hook) not in {_dedupe_key(item) for item in (claim, insight)}:
        add(_sentence_case_words(_clause(hook, max_words=14)), "direct", "Uses the strongest self-contained line from the clip.", hook)

    # A narrow clip sometimes yields only three distinct editorial angles. Fill
    # the rest from other explicit statements, not by rewording the same line.
    used = {_dedupe_key(item["text"]) for item in raw}
    for sentence in clip_sentences:
        if len(raw) >= 8:
            break
        text = _strip_end(_clause(str(sentence.get("text") or ""), max_words=12))
        if len(text.split()) < 4 or _dedupe_key(text) in used:
            continue
        angle = "story" if re.search(r"\b(i|we)\b", text, re.I) else "descriptive"
        rationale = "Uses another explicit statement from the clip, not the opening line."
        add(_sentence_case_words(text), angle, rationale, str(sentence.get("text") or ""))
        used.add(_dedupe_key(text))

    if requested_angle in TITLE_ANGLES:
        raw = [item for item in raw if item["angle"] == requested_angle] + [
            item for item in raw if item["angle"] != requested_angle
        ]
    scored = score_titles(
        raw,
        brief=brief,
        clip_full_text=" ".join(str(item.get("text") or "") for item in clip_sentences),
        clip_sentences=clip_sentences,
        platform=platform,
        settings=settings,
        research=research,
    )
    return scored


def _sentence_list(brief: ContentBrief) -> list[str]:
    return [item.text for item in brief.evidence if item.in_clip]


def descriptions(
    brief: ContentBrief,
    *,
    platform: str,
    settings: dict,
    research: KeywordResearchResult | None = None,
    selected_title: str = "",
) -> DescriptionVariants:
    profile = packaging_profile(platform)
    short_limit = int(profile.get("description_short_max") or 150)
    long_limit = int(profile.get("description_long_max") or 2_200)
    style = str(settings.get("description_style") or "auto")
    long_form = platform == "youtube" or style == "long_form"

    subject = brief.clip_topic or brief.subject or "this clip"
    claim = _strip_end(brief.central_claim)
    insight = _strip_end(brief.key_insight or brief.central_claim)
    takeaway = _strip_end(brief.practical_takeaway or brief.key_insight or brief.central_claim)
    detail = _strip_end(brief.example_evidence[0]) if brief.example_evidence else ""
    caveat = _strip_end(brief.caveats[0]) if brief.caveats else ""

    opening = f"This clip looks at {subject}."
    short_parts = [opening]
    # A caveat such as "this only works if..." refers to something the viewer has
    # not read yet, so the short description leads with the central claim when it
    # can stand alone.
    point = next(
        (
            value
            for value in (claim, insight, takeaway)
            if value and not _starts_with_reference(value)
        ),
        "",
    )
    if point:
        short_parts.append(f"It makes the case that {_lower_first(_strip_lead(point))}.")
    short = _trim(" ".join(short_parts), short_limit)
    if len(short) < 40 and point:
        short = _trim(f"{opening} {_sentence_case_words(_strip_lead(point))}.", short_limit)

    parts: list[str] = [opening]
    if claim:
        parts.append(f"The clip's main point is that {_lower_first(claim)}.")
    if detail and _dedupe_key(detail) != _dedupe_key(claim):
        parts.append(f"It uses a concrete example: {_lower_first(detail)}.")
    if takeaway and _dedupe_key(takeaway) != _dedupe_key(claim):
        parts.append(f"The practical takeaway is to {_lower_first(_strip_lead(takeaway))}.")
    if caveat and _dedupe_key(caveat) not in {_dedupe_key(claim), _dedupe_key(takeaway)}:
        parts.append(
            f"The speaker adds one qualification: {_lower_first(_strip_lead(caveat))}."
        )
    if long_form and brief.wider_context:
        parts.append("From the wider conversation, not this clip: " + _strip_end(brief.wider_context[0]) + ".")
    long_text = _trim(" ".join(parts), long_limit)
    return DescriptionVariants(
        short=short,
        long=long_text,
        short_limit=short_limit,
        long_limit=long_limit,
        within_limit=len(short) <= short_limit and len(long_text) <= long_limit,
    )


THUMB_DANGLING = {
    "the", "a", "an", "for", "to", "of", "and", "or", "in", "on", "with", "that", "this",
    "these", "those", "you", "we", "they", "it", "he", "she", "is", "are", "was", "were",
    "be", "been", "your", "our", "their", "at", "by", "from", "than", "as", "if", "when",
    "how", "what", "why", "who", "which", "but", "so", "because",
}


def thumbnail_texts(brief: ContentBrief, *, platform: str, limit: int | None = None) -> list[str]:
    profile = packaging_profile(platform)
    cap = limit or int(profile.get("thumbnail_text_max") or 40)
    options: list[str] = []

    def clean_thumb(value: str) -> str:
        # A thumbnail has no antecedent, so a phrase such as "this only works"
        # cannot stand on its own.
        if not value or _starts_with_reference(value):
            return ""
        text = _strip_lead(value)
        for prefix in ("you should ", "you need to ", "you have to ", "i think ", "the real problem is "):
            if text.lower().startswith(prefix):
                text = text[len(prefix):]
        text = re.sub(r"^(the|a|an)\s+", "", text, flags=re.I)
        text = _strip_end(text)
        # The platform limit is a character budget, not a word count: use the
        # room available before giving up a meaningful word.
        text = _trim(text, cap)
        tokens = text.split()
        while tokens and tokens[-1].lower().strip(".,;:!?") in THUMB_DANGLING:
            tokens.pop()
        text = _sentence_case_words(_strip_end(" ".join(tokens)))
        return text if len(text.split()) >= 2 else ""

    for value in (
        brief.central_claim,
        brief.key_insight,
        brief.practical_takeaway,
    ):
        text = clean_thumb(value)
        if not text or len(text) > cap:
            continue
        if text.lower() in {item.lower() for item in options}:
            continue
        options.append(text)
    primary = options[0].lower() if options else ""
    return [
        option
        for option in options
        if option.lower() == primary or not primary.startswith(option.lower())
    ][:3]


def content_angle(brief: ContentBrief, titles: list[TitleVariant]) -> str:
    selected = next((item for item in titles if item.accepted), titles[0] if titles else None)
    if not selected:
        return ""
    labels = {
        "descriptive": "Clear explanation of the clip's subject.",
        "insight": "Insight-led, built around the most specific idea.",
        "problem_solution": "Problem-and-solution framing.",
        "question": "Question-led framing.",
        "benefit": "Takeaway-led framing.",
        "contrarian": "Qualification-led framing, only where the source supports it.",
        "story": "Example-led framing.",
        "search": "Topic phrase paired with the clip's point.",
        "direct": "Direct line from the clip.",
    }
    return labels.get(selected.angle, selected.reason or "")
