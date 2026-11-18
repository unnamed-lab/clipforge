"""Source-grounded content briefs for packaging.

The brief is the missing layer between transcription and marketing copy. It
reads the whole video, builds section summaries, retrieves the sections around
and related to the selected clip, and then describes the clip from its own
evidence. It never assumes the opening sentence is the important one.

The builder is deterministic and offline. Long sources are handled
hierarchically: sections are summarised first, then only the clip, its
surrounding context and the most relevant other sections are assembled into the
brief. ``omitted_sections`` and ``truncated`` record what was left out rather
than pretending the model saw everything.
"""

from __future__ import annotations

import hashlib
import json
import re

from clipforge.packaging.generator import STOPWORDS, ClipSentence, ClipText, clip_text
from clipforge.packaging.models import BRIEF_VERSION, BriefEvidence, ContentBrief, SectionSummary

MAX_SECTION_CHARS = 620
MAX_SECTION_SENTENCES = 8
SECTION_GAP_MS = 2_800
CONTEXT_PAD_MS = 30_000
MAX_CLIP_CONTEXT_MS = 180_000

CAVEAT_MARKERS = (
    " but ", " however ", " unless ", " except ", " only if ", " depends ", " though ",
    " not always ", " caveat ", " may ", " might ", " in some cases ", " it's not ",
    " it is not ", " doesn't always ", " does not always ",
)
EXAMPLE_MARKERS = (
    " for example ", " for instance ", " one time ", " when i ", " when we ", " we spent ",
    " i spent ", " i remember ", " we tried ", " i tried ", " last year ", " a few years ",
    " recently ", " the other day ",
)
IMPERATIVE_VERBS = {
    "talk", "start", "write", "use", "try", "hire", "build", "measure", "ask", "check",
    "focus", "stop", "avoid", "pick", "choose", "test", "read", "listen", "watch", "learn",
    "make", "take", "keep", "set", "plan", "review", "track", "ship", "practice", "practise",
}
CLAIM_MARKERS = (
    " because ", " so that ", " therefore ", " means ", " the reason ", " the point ",
    " the key ", " the problem ", " the mistake ", " the lesson ", " actually ", " should ",
    " need to ", " have to ", " must ", " never ", " always ", " the trick ", " the idea ",
    " turns out ", " i learned ", " we learned ",
)
STORY_MARKERS = (
    " when i ", " when we ", " i was ", " we were ", " i had ", " we had ", " i spent ",
    " we spent ", " i started ", " we started ", " i realised ", " i realized ", " we realised ",
    " we realized ", " i remember ", " we remember ",
)
DEICTIC_OPENERS = {
    "this", "that", "these", "those", "it", "they", "he", "she", "him", "her", "them",
    "so", "and", "but", "then", "there", "here",
}
TOPIC_SHIFT_MARKERS = (
    "now let us", "now let's", "after the break", "next up", "let's move on", "let us move on",
    "moving on", "on to the next", "another topic", "switch to", "switching to", "finally",
    "in summary", "to summarise", "to summarize", "second topic", "third topic",
)
LOW_INFORMATION = {
    "thing", "things", "stuff", "something", "anything", "everything", "nothing",
    "guys", "okay", "yeah", "um", "uh", "like", "know", "mean",
}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _tokens(value: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", value or "")
    return [re.sub(r"['-]", "", word.lower()) for word in words]


def _content_tokens(value: str) -> set[str]:
    return {token for token in _tokens(value) if token not in STOPWORDS and token not in LOW_INFORMATION}


def _proper_nouns(value: str) -> list[str]:
    """Names, products and organisations that must survive editing intact."""
    found: list[str] = []
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9&.'-]{1,}|[A-Z]{2,})\b", value or ""):
        candidate = match.group(1).strip(".'-")
        lower = candidate.lower()
        if lower in STOPWORDS or lower in LOW_INFORMATION or len(candidate) < 2:
            continue
        if lower in {"i", "we", "you", "they", "he", "she", "it", "the", "a", "an"}:
            continue
        found.append(candidate)
    return list(dict.fromkeys(found))


def _sentence_rows(transcript: dict) -> list[dict]:
    """Flatten transcript segments into sentence-like rows with source time."""
    rows: list[dict] = []
    for segment in transcript.get("segments") or []:
        words = segment.get("words") or []
        if words:
            for word in words:
                try:
                    start = int(word.get("start_ms") or 0)
                    end = int(word.get("end_ms") or start)
                except (TypeError, ValueError):
                    continue
                rows.append(
                    {
                        "word": str(word.get("word") or ""),
                        "start_ms": start,
                        "end_ms": end,
                        "segment_id": segment.get("id"),
                        "speaker": segment.get("speaker"),
                        "probability": word.get("probability"),
                    }
                )
        elif segment.get("text"):
            rows.append(
                {
                    "word": str(segment.get("text")),
                    "start_ms": int(segment.get("start_ms") or 0),
                    "end_ms": int(segment.get("end_ms") or 0),
                    "segment_id": segment.get("id"),
                    "speaker": segment.get("speaker"),
                    "probability": None,
                }
            )
    return rows


def _sentence_list(rows: list[dict]) -> list[dict]:
    sentences: list[dict] = []
    buffer: list[dict] = []

    def flush() -> None:
        nonlocal buffer
        text = clean(" ".join(str(row["word"]) for row in buffer))
        if text:
            probabilities = [row["probability"] for row in buffer if isinstance(row.get("probability"), (int, float))]
            sentences.append(
                {
                    "text": text,
                    "start_ms": int(buffer[0]["start_ms"]),
                    "end_ms": int(buffer[-1]["end_ms"]),
                    "segment_id": buffer[0].get("segment_id"),
                    "speaker": buffer[0].get("speaker"),
                    "probability": (sum(probabilities) / len(probabilities)) if probabilities else None,
                }
            )
        buffer = []

    for row in rows:
        buffer.append(row)
        token = str(row["word"]).strip()
        if token.endswith((".", "!", "?")) or len(buffer) >= 30:
            flush()
    flush()
    return sentences


def _sectionise(sentences: list[dict]) -> list[dict]:
    sections: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        nonlocal current
        if current:
            sections.append(
                {
                    "id": f"s{len(sections) + 1}",
                    "start_ms": current[0]["start_ms"],
                    "end_ms": current[-1]["end_ms"],
                    "sentences": list(current),
                }
            )
        current = []

    for sentence in sentences:
        if current:
            gap = sentence["start_ms"] - current[-1]["end_ms"]
            chars = sum(len(item["text"]) for item in current)
            speaker_changed = (
                sentence.get("speaker") is not None
                and current[-1].get("speaker") is not None
                and sentence["speaker"] != current[-1]["speaker"]
            )
            topic_shift = sentence["text"].lower().lstrip().startswith(TOPIC_SHIFT_MARKERS)
            if gap > SECTION_GAP_MS or chars >= MAX_SECTION_CHARS or len(current) >= MAX_SECTION_SENTENCES or speaker_changed or topic_shift:
                flush()
        current.append(sentence)
    flush()
    return sections


def _claim_score(sentence: dict) -> float:
    text = f" {sentence['text'].lower()} "
    score = 0.0
    if re.search(r"\b\d[\d.,]*\b", text):
        score += 3.0
    score += 2.0 * len(_proper_nouns(sentence["text"]))
    score += 2.0 * sum(1 for marker in CLAIM_MARKERS if marker in text)
    if text.strip().endswith("?"):
        score += 1.0
    if any(marker in text for marker in EXAMPLE_MARKERS):
        score += 1.5
    if any(marker in text for marker in CAVEAT_MARKERS):
        score += 0.5
    words = len(text.split())
    if words < 5:
        score -= 2.0
    if words > 40:
        score -= 1.0
    if text.strip().split(" ")[1:3] and text.strip().split(" ")[1] in {"so", "yeah", "um", "uh"}:
        score -= 1.0
    return score


def _hook_score(sentence: dict, *, first_in_clip: bool) -> float:
    text = f" {sentence['text'].lower()} "
    score = _claim_score(sentence)
    words = text.split()
    if not words:
        return -10.0
    opener = words[0].strip(".,!?")
    if opener in DEICTIC_OPENERS:
        score -= 1.5
    if first_in_clip:
        # Opening position is a small production preference, not a reason to
        # treat the first sentence as the clip's best idea.
        score += 0.25
    if len(words) > 8:
        score += 1.0
    if len(words) > 34:
        score -= 1.0
    return score


def _best(sentences: list[dict], *, exclude: set[str] | None = None, predicate=None) -> dict | None:
    excluded = exclude or set()
    considered = [item for item in sentences if item["text"] not in excluded and (predicate(item) if predicate else True)]
    if not considered:
        return None
    return max(considered, key=_claim_score)


def _section_summary(section: dict) -> tuple[str, list[str]]:
    sentences = section["sentences"]
    ranked = sorted(sentences, key=_claim_score, reverse=True)
    chosen: list[dict] = []
    for sentence in ranked:
        if len(chosen) >= 2:
            break
        if any(sentence["text"] == item["text"] for item in chosen):
            continue
        chosen.append(sentence)
    chosen.sort(key=lambda item: item["start_ms"])
    summary = clean(" ".join(item["text"] for item in chosen))[:420]
    counts: dict[str, int] = {}
    for sentence in sentences:
        for token in _content_tokens(sentence["text"]):
            counts[token] = counts.get(token, 0) + 1
    key_terms = [term for term, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:6]]
    return summary, key_terms


def _clip_window(candidate: dict) -> tuple[int, int]:
    starts = []
    ends = []
    editorial = candidate.get("editorial") or {}
    decision = editorial.get("editorial") or {}
    timeline = editorial.get("timeline") or decision.get("timeline") or {}
    if decision.get("applied") and timeline.get("valid"):
        for entry in timeline.get("entries") or []:
            start = int(entry.get("source_start_ms") or 0)
            end = int(entry.get("source_end_ms") or 0)
            if end > start:
                starts.append(start)
                ends.append(end)
    if not starts:
        start = int(candidate.get("user_start_ms") or candidate.get("start_ms") or 0)
        end = int(candidate.get("user_end_ms") or candidate.get("end_ms") or start)
        starts.append(start)
        ends.append(max(start, end))
    return min(starts), max(ends)


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def brief_key(transcript: dict, candidate: dict, *, source_title: str | None = None) -> str:
    """Stable cache key independent of platform and presentation settings."""
    editorial = candidate.get("editorial") or {}
    payload = {
        "brief_version": BRIEF_VERSION,
        "transcript": transcript.get("id"),
        "start": candidate.get("start_ms"),
        "end": candidate.get("end_ms"),
        "user_start": candidate.get("user_start_ms"),
        "user_end": candidate.get("user_end_ms"),
        "timeline": (editorial.get("timeline") or {}).get("entries") or [],
        "applied": (editorial.get("editorial") or {}).get("applied"),
        "source_title": source_title or "",
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return digest[:32]


def _clip_sentences(text: ClipText) -> list[dict]:
    return [
        {
            "text": sentence.text,
            "start_ms": sentence.start_ms,
            "end_ms": sentence.end_ms,
            "segment_id": None,
            "speaker": None,
            "probability": None,
            "sentence_id": sentence.sentence_id,
        }
        for sentence in text.sentences
    ]


def _clip_terms(sentences: list[ClipSentence]) -> set[str]:
    values: set[str] = set()
    for sentence in sentences:
        values |= _content_tokens(sentence.text)
    return values


def _classify_clip(
    clip: list[dict],
    *,
    video_terms: set[str],
    clip_terms: set[str],
    context_needed: bool,
) -> str:
    text = " ".join(f" {item['text'].lower()} " for item in clip)
    first = clip[0]["text"].strip() if clip else ""
    first_word = re.sub(r"[^A-Za-z]", "", first.split(" ")[0]).lower() if first else ""
    imperative = sum(
        1
        for item in clip
        if item["text"].strip().split(" ")
        and re.sub(r"[^A-Za-z]", "", item["text"].strip().split(" ")[0]).lower() in IMPERATIVE_VERBS
    )
    story = sum(1 for marker in STORY_MARKERS if marker in text)
    caveats = sum(1 for marker in CAVEAT_MARKERS if marker in text)
    questions = text.count("?")
    if context_needed or first_word in DEICTIC_OPENERS:
        return "excerpt_needs_context"
    if imperative >= 2 or (" first " in text and " then " in text):
        return "practical_tutorial"
    if story >= 2:
        return "personal_story"
    if caveats >= 2 and (" but " in text or " however " in text):
        return "debate"
    if (" i think " in text or " i believe " in text or " in my opinion " in text) and questions <= 1:
        return "reaction_opinion"
    if sum(1 for marker in CLAIM_MARKERS if marker in text) >= 2 and _overlap(clip_terms, video_terms) >= 0.2:
        return "concept_explanation"
    if questions:
        return "concept_explanation"
    if _overlap(clip_terms, video_terms) < 0.5:
        return "highlight"
    return "standalone_insight"


def _confidence(clip: list[dict], *, ambiguity: list[str], truncated: bool) -> str:
    if truncated or ambiguity:
        return "low" if len(ambiguity) >= 2 else "medium"
    probabilities = [item["probability"] for item in clip if isinstance(item.get("probability"), (int, float))]
    if probabilities and sum(probabilities) / len(probabilities) < 0.55:
        return "low"
    if len(" ".join(item["text"] for item in clip)) < 180:
        return "low"
    return "high"


def _evidence_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index}"


def _clip_topic_phrase(
    *,
    source_title: str | None,
    claim: str,
    insight: str,
    top_terms: list[str],
    clip_terms: set[str],
) -> str:
    """Prefer a human-readable phrase that is supported by the clip."""
    segments = [
        clean(part)
        for part in re.split(r"[:\-–—|,]", source_title or "")
        if clean(part)
    ]
    if segments:
        scored = []
        for segment in segments:
            words = [word for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", segment)]
            if not words:
                continue
            overlap = len({word.lower() for word in words} & clip_terms)
            scored.append((overlap / len(words), len(words), segment))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if scored and scored[0][0] >= 0.5:
            value = re.sub(r"^(the|a|an)\s+", "", scored[0][2], flags=re.I)
            return value[:80]
    for value in (claim, insight):
        text = clean(value)
        match = re.search(r"\b(?:about|on|with|for)\s+([a-z][a-z\s'-]{3,40})", text, re.I)
        if match:
            phrase = clean(match.group(1)).split(".")
            if phrase and len(phrase[0].split()) >= 2:
                return re.sub(r"^(the|a|an)\s+", "", phrase[0], flags=re.I)[:80]
    # A single supported term is honest; joining unrelated top terms invents a
    # phrase no one said ("always cost customers").
    if top_terms:
        return top_terms[0][:80]
    return "clip"


FRAGMENT_WORDS = {
    # Verb forms that turn an adjacent pair into a fragment ("founders make",
    # "we spent") rather than a concept someone would search for.
    "make", "makes", "made", "spend", "spends", "spent", "say", "says", "said",
    "tell", "told", "get", "gets", "got", "go", "goes", "went", "come", "comes",
    "came", "take", "takes", "took", "give", "gives", "gave", "try", "tries",
    "tried", "want", "wants", "think", "thinks", "thought", "knew", "keep",
    "keeps", "kept", "put", "look", "looks", "looked", "start", "starts",
    "started", "stop", "stops", "stopped", "return", "returns", "returned",
}

NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "hundred",
    "thousand", "million", "billion",
}


GERUND_NOUNS = {
    # "-ing" words that name a discipline or activity and read as search terms.
    "accounting", "banking", "budgeting", "building", "coaching", "coding",
    "consulting", "editing", "engineering", "forecasting", "fundraising", "gaming",
    "hiring", "investing", "learning", "marketing", "measuring", "negotiating",
    "networking", "onboarding", "planning", "pricing", "publishing", "recruiting",
    "streaming", "trading", "training", "writing",
}


def _concept_phrases(sentences: list[dict], top_terms: list[str]) -> list[str]:
    """Two-word concepts are more useful as search phrases than bare words."""
    counts: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    for sentence in sentences:
        # Keep short words so adjacency is literal: dropping "to" turns "talk to
        # customers" into the non-phrase "talk customers".
        tokens = re.findall(r"[A-Za-z][A-Za-z'’-]*", sentence.get("text") or "")
        run: list[str] = []
        for token in tokens + [""]:
            word = re.sub(r"['’-]s?$", "", token.lower()).strip("'’-")
            if (
                not word
                or word in STOPWORDS
                or word in LOW_INFORMATION
                or word in FRAGMENT_WORDS
                or word in NUMBER_WORDS
            ):
                # A phrase may not bridge a dropped word: "six months looking"
                # must never become "months looking".
                run = []
                continue
            run.append(word)
            if len(run) >= 2:
                # "months looking" is a verb phrase caught mid-sentence, not a
                # concept; discipline nouns such as "machine learning" stay.
                if run[-1].endswith("ing") and run[-1] not in GERUND_NOUNS:
                    continue
                phrase = f"{run[-2]} {run[-1]}"
                counts[phrase] = counts.get(phrase, 0) + 1
                first_seen.setdefault(phrase, len(first_seen))
    ranked = [
        phrase
        for phrase, _ in sorted(
            counts.items(), key=lambda item: (-item[1], first_seen[item[0]])
        )
    ]
    return ranked[:4]


def build_content_brief(
    transcript: dict,
    candidate: dict,
    *,
    source_title: str | None = None,
    source_description: str | None = None,
    settings: dict | None = None,
    platform: str = "cross_platform",
) -> ContentBrief:
    """Build a source-grounded brief from the full transcript and the clip.

    The clip is always the subject of the brief's clip-level fields. Material
    from the rest of the video is retained as ``wider_context`` and labelled.
    """
    settings = settings or {}
    text = clip_text(transcript, candidate)
    clip = _clip_sentences(text)
    warnings: list[str] = list(text.warnings)

    all_sentences = _sentence_list(_sentence_rows(transcript))
    if not all_sentences:
        all_sentences = list(clip)
        warnings.append("The source transcript had no usable words; the brief is limited to the clip excerpt.")

    clip_start, clip_end = _clip_window(candidate)
    sections = _sectionise(all_sentences)
    clip_terms = _clip_terms(text.sentences)
    video_terms: set[str] = set()
    for section in sections:
        for sentence in section["sentences"]:
            video_terms |= _content_tokens(sentence["text"])

    section_summaries: list[SectionSummary] = []
    selected_context: list[dict] = []
    omitted = 0
    for section in sections:
        summary, key_terms = _section_summary(section)
        contains = section["start_ms"] < clip_end and section["end_ms"] > clip_start
        near = section["end_ms"] >= clip_start - MAX_CLIP_CONTEXT_MS and section["start_ms"] <= clip_end + MAX_CLIP_CONTEXT_MS
        relevance = _overlap(set(key_terms), clip_terms | video_terms)
        if contains:
            relevance = 1.0
        elif near:
            relevance = max(relevance, 0.45)
        section_summaries.append(
            SectionSummary(
                id=section["id"],
                label=key_terms[0] if key_terms else "",
                start_ms=section["start_ms"],
                end_ms=section["end_ms"],
                summary=summary,
                key_terms=key_terms,
                contains_clip=contains,
                relevance=round(relevance, 3),
            )
        )
        if contains or near:
            selected_context.append(section)
        elif relevance >= 0.3:
            selected_context.append(section)
        else:
            omitted += 1

    # Keep prompt context bounded but deterministic: the clip, adjacent content
    # and up to two strongly related sections. Omitted sections are counted.
    selected_context.sort(key=lambda item: (item["start_ms"] > clip_end, item["start_ms"]))
    budget_sections = selected_context[:10]
    if len(selected_context) > len(budget_sections):
        omitted += len(selected_context) - len(budget_sections)
    context_chars = sum(len(item["text"]) for section in budget_sections for item in section["sentences"])

    if not clip:
        clip = [
            {
                "text": clean(candidate.get("excerpt") or candidate.get("summary") or "Clip"),
                "start_ms": clip_start,
                "end_ms": clip_end,
                "probability": None,
            }
        ]
    else:
        # Attach any measured word confidence from the full transcript by overlap.
        by_text = {item["text"]: item for item in all_sentences}
        for item in clip:
            match = by_text.get(item["text"])
            if match:
                item["probability"] = match.get("probability")

    ranked_clip = sorted(
        [
            {**item, "claim_score": _claim_score(item), "hook_score": _hook_score(item, first_in_clip=index == 0)}
            for index, item in enumerate(clip)
        ],
        key=lambda item: (-item["hook_score"], -item["claim_score"], item["start_ms"]),
    )
    hook = ranked_clip[0] if ranked_clip else None
    claim_sentences = sorted(ranked_clip, key=lambda item: (-item["claim_score"], item["start_ms"]))
    central = claim_sentences[0] if claim_sentences else None
    insight = next(
        (
            item
            for item in claim_sentences
            if item is not central and len(_content_tokens(item["text"])) >= 3
        ),
        central,
    )

    caveats = [
        item["text"]
        for item in clip
        if any(marker in f" {item['text'].lower()} " for marker in CAVEAT_MARKERS)
    ][:3]
    examples = [
        item["text"]
        for item in clip
        if any(marker in f" {item['text'].lower()} " for marker in EXAMPLE_MARKERS)
        or re.search(r"\b\d[\d.,]*\b", item["text"])
    ][:3]
    supporting = [
        item["text"]
        for item in claim_sentences
        if item["text"] not in {central["text"] if central else "", insight["text"] if insight else ""}
    ][:4]

    problem_sentence = _best(
        clip,
        predicate=lambda item: any(
            marker in f" {item['text'].lower()} "
            for marker in (" problem ", " struggle ", " mistake ", " hard ", " difficult ", " challenge ")
        ),
    )
    takeaway_sentence = _best(
        clip,
        predicate=lambda item: (
            re.sub(r"[^A-Za-z]", "", item["text"].strip().split(" ")[0]).lower() in IMPERATIVE_VERBS
            or " should " in f" {item['text'].lower()} "
            or " need to " in f" {item['text'].lower()} "
            or " have to " in f" {item['text'].lower()} "
        ),
    )

    entities = _proper_nouns(" ".join(item["text"] for item in clip))

    counts: dict[str, int] = {}
    for item in clip:
        for token in _content_tokens(item["text"]):
            counts[token] = counts.get(token, 0) + 1
    top_terms = [term for term, _ in sorted(counts.items(), key=lambda entry: (-entry[1], entry[0]))]
    clip_topic = _clip_topic_phrase(
        source_title=source_title,
        claim=central["text"] if central else "",
        insight=insight["text"] if insight else "",
        top_terms=top_terms,
        clip_terms=clip_terms,
    ) or clean(str(settings.get("niche") or "")) or "clip"
    video_counts: dict[str, int] = {}
    for section in sections:
        for sentence in section["sentences"]:
            for token in _content_tokens(sentence["text"]):
                video_counts[token] = video_counts.get(token, 0) + 1
    video_terms_sorted = [term for term, _ in sorted(video_counts.items(), key=lambda entry: (-entry[1], entry[0]))]
    video_topic = clean(" ".join(video_terms_sorted[:4])) or clip_topic

    clip_start_text = clip[0]["text"].strip() if clip else ""
    context_needed = bool(
        re.match(r"^(so|and|but|then|it|this|that|they|he|she|those|these)\b", clip_start_text, re.I)
        or re.search(r"\b(earlier|as i said|before that|we talked about|like i said)\b", clip_start_text, re.I)
        or candidate.get("boundary", {}).get("needs_context")
    )
    clip_type = _classify_clip(
        clip,
        video_terms=video_terms,
        clip_terms=clip_terms,
        context_needed=context_needed,
    )

    ambiguity: list[str] = []
    if context_needed:
        ambiguity.append("The clip opens with a reference that may need earlier context.")
    if not central:
        ambiguity.append("No clear central claim could be identified from the clip transcript.")
    if text.timeline_applied is False and (candidate.get("editorial") or {}).get("timeline", {}).get("entries"):
        ambiguity.append("A non-linear editorial plan exists but is not applied; packaging follows the approved clip window.")
    if any(item.get("probability") is not None and item["probability"] < 0.45 for item in clip):
        ambiguity.append("Parts of the clip transcript have low word confidence; verify names and numbers.")

    wider_context: list[str] = []
    for section in section_summaries:
        if section.contains_clip:
            continue
        if section.relevance >= 0.35:
            wider_context.append(section.summary)
        if len(wider_context) >= 2:
            break
    minor_topics = [
        term
        for section in section_summaries
        if not section.contains_clip
        for term in section.key_terms[:2]
        if term not in clip_terms
    ][:4]

    search_concepts: list[str] = []
    for value in [clip_topic, *entities[:3], *_concept_phrases(clip, top_terms), *top_terms[:3]]:
        cleaned = clean(value)
        if len(cleaned) < 3:
            continue
        if cleaned.lower() not in {item.lower() for item in search_concepts}:
            search_concepts.append(cleaned)
    search_concepts = search_concepts[:8]

    evidence: list[BriefEvidence] = []
    for index, item in enumerate(clip):
        evidence.append(
            BriefEvidence(
                id=_evidence_id("clip", index + 1),
                text=item["text"],
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                kind="hook" if hook and item["text"] == hook["text"] else "claim",
                sentence_id=item.get("sentence_id"),
                in_clip=True,
            )
        )
    for index, section in enumerate(section_summaries):
        if section.contains_clip or section.relevance < 0.35:
            continue
        evidence.append(
            BriefEvidence(
                id=_evidence_id("context", index + 1),
                text=section.summary,
                start_ms=section.start_ms,
                end_ms=section.end_ms,
                kind="context",
                section_id=section.id,
                in_clip=False,
            )
        )

    hook_id = None
    if hook:
        for item in evidence:
            if item.text == hook["text"] and item.in_clip:
                hook_id = item.id
                break

    subject = clean(str(settings.get("niche") or "")) or clean(source_title or "") or clip_topic
    brief = ContentBrief(
        version=BRIEF_VERSION,
        method="deterministic",
        brief_key=brief_key(transcript, candidate, source_title=source_title),
        clip_type=clip_type,  # type: ignore[arg-type]
        subject=subject[:160],
        video_topic=video_topic[:160],
        clip_topic=clip_topic[:160],
        central_claim=clean(central["text"])[:420] if central else "",
        key_insight=clean(insight["text"])[:420] if insight else "",
        audience_problem=clean(problem_sentence["text"])[:320] if problem_sentence else "",
        practical_takeaway=clean(takeaway_sentence["text"])[:320] if takeaway_sentence else "",
        supporting_points=supporting,
        example_evidence=examples,
        caveats=caveats,
        stronger_hook=clean(hook["text"])[:320] if hook else "",
        hook_evidence_id=hook_id,
        entities=entities[:12],
        search_concepts=search_concepts,
        minor_topics=list(dict.fromkeys(minor_topics)),
        wider_context=wider_context,
        evidence=evidence,
        sections=section_summaries,
        source_title=clean(source_title or "")[:300] or None,
        source_description=clean(source_description or "")[:1000] or None,
        confidence=_confidence(clip, ambiguity=ambiguity, truncated=omitted > 0 and context_chars > 12_000),
        ambiguity=ambiguity,
        warnings=list(dict.fromkeys([*warnings, "Briefs are deterministic extracts; verify names, numbers and qualifiers before publishing."])),
        context_chars=context_chars,
        omitted_sections=omitted,
        truncated=context_chars > 12_000,
    )
    return brief


def brief_context_payload(brief: ContentBrief, *, max_chars: int = 9_000) -> dict:
    """Compact, evidence-labelled brief for a model prompt."""
    evidence = [
        {"id": item.id, "text": item.text[:320], "kind": item.kind, "start_ms": item.start_ms, "in_clip": item.in_clip}
        for item in brief.evidence
    ]
    payload = {
        "brief_version": brief.version,
        "clip_type": brief.clip_type,
        "subject": brief.subject,
        "video_topic": brief.video_topic,
        "clip_topic": brief.clip_topic,
        "central_claim": brief.central_claim,
        "key_insight": brief.key_insight,
        "audience_problem": brief.audience_problem,
        "practical_takeaway": brief.practical_takeaway,
        "supporting_points": brief.supporting_points,
        "examples": brief.example_evidence,
        "caveats": brief.caveats,
        "strongest_hook": brief.stronger_hook,
        "entities": brief.entities,
        "search_concepts": brief.search_concepts,
        "minor_topics": brief.minor_topics,
        "wider_context": brief.wider_context,
        "evidence": evidence,
        "confidence": brief.confidence,
        "ambiguity": brief.ambiguity,
        "truncated": brief.truncated,
        "omitted_sections": brief.omitted_sections,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) <= max_chars:
        return payload
    payload["evidence"] = evidence[:8]
    payload["supporting_points"] = brief.supporting_points[:3]
    payload["wider_context"] = brief.wider_context[:1]
    payload["truncated"] = True
    return payload


def validate_brief_claims(brief: ContentBrief, clip_full_text: str) -> list[str]:
    """Check that clip-level fields are actually supported by the clip text."""
    source = (clip_full_text or "").lower()
    issues: list[str] = []
    for field, value, in_clip_only in (
        ("central_claim", brief.central_claim, True),
        ("key_insight", brief.key_insight, True),
        ("strongest_hook", brief.stronger_hook, True),
        ("practical_takeaway", brief.practical_takeaway, True),
    ):
        text = clean(value)
        if not text or not in_clip_only:
            continue
        core = text.rstrip(".").lower()
        if len(core) > 24 and core not in source:
            # Exact quotation is required for deterministic brief fields; an LLM
            # brief is allowed to paraphrase, but its numbers and names are
            # still checked by the packaging validator.
            issues.append(f"{field} is not quoted from the selected clip.")
    for name in brief.entities:
        if name.lower() not in source and name.lower() not in (brief.source_title or "").lower():
            issues.append(f"Entity '{name}' is not present in the clip transcript.")
    return issues
