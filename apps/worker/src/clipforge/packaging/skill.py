"""Versioned editorial skill: ``clipforge-packaging-director``.

This is the repository's documented prompt module and schema contract. It is
deliberately provider-agnostic: the same instructions are used for local models
and hosted OpenAI-compatible endpoints, while transport and credentials stay in
``clipforge.providers``.
"""

from __future__ import annotations

import json
import os

from clipforge.packaging.brief import brief_context_payload
from clipforge.packaging.models import ContentBrief, KeywordResearchResult
from clipforge.platforms import packaging_profile

SKILL_ID = "clipforge-packaging-director"
SKILL_VERSION = "1.0.0"
PROMPT_VERSION = "packaging-director-prompt/1.0"

TITLE_ANGLES = (
    "descriptive",
    "insight",
    "problem_solution",
    "question",
    "benefit",
    "contrarian",
    "story",
    "search",
)

DEFAULT_TITLE_WEIGHTS = {
    "accuracy": 0.30,
    "specificity": 0.20,
    "audience_relevance": 0.20,
    "curiosity": 0.15,
    "search_relevance": 0.10,
    "readability": 0.05,
}

# Accuracy is a gate, not a trade-off. A title that fails it is rejected even if
# every other component scores well.
ACCURACY_GATE = 0.65

SYSTEM_PROMPT = """You are the Clipforge packaging director. You write accurate, specific metadata for one clip cut from a longer spoken-word video.

SOURCE RULES
- The selected clip is the primary subject. The full video provides context only.
- Use only facts, names, numbers, quotations and claims supported by the brief evidence.
- Evidence marked in_clip=true may be described as being in the clip. Wider context (in_clip=false) may only be used as clearly labelled background, never as clip content.
- Never invent a name, product, organisation, statistic, quotation, credential, source, link, trend metric or promise.
- If a fact is uncertain or the transcript is ambiguous, say less rather than guessing.
- A quotation is only a quotation if it is word-for-word from the evidence.

TITLE RULES
- Produce 5-8 genuinely different candidates, not eight rewordings of one sentence.
- Use different angles where the source supports them: descriptive, insight, problem_solution, question, benefit, contrarian, story, search.
- Express the clip's actual subject or strongest supported insight. Prefer concrete language.
- Do not merely repeat the first transcript sentence.
- Do not use misleading clickbait, fake controversy, invented urgency or generic phrases such as "You won't believe this".
- Respect the creator's tone, language and forbidden terms.
- Stay inside the supplied platform limits. Do not stuff keywords.
- Every candidate needs a short rationale, the evidence ids it relies on, and any limitation.

DESCRIPTION RULES
- Explain what the clip is about, the specific point it addresses, what the viewer will learn or see, and one relevant supporting detail.
- Do not copy the transcript verbatim and do not repeat one idea in several paragraphs.
- Add wider context only with an explicit label such as "From the wider conversation:".
- No invented links, sources, timestamps, guest credentials or calls to action.
- Use the creator's configured CTA only. Do not add generic subscribe/buy copy.
- Adapt length and structure to the target platform and to short-form or long-form style.

OTHER FIELDS
- Hashtags and keywords are suggestions. Never claim they guarantee reach or that any metric was measured unless the supplied research says so.
- Thumbnail text communicates one idea and complements the title instead of repeating it. Return text and composition suggestions only; never claim an image was created.
- If no research was supplied, do not imply live trend or search-volume knowledge.

OUTPUT
Return STRICT JSON only, no markdown fences, matching:
{
  "brief_assessment": {"clip_type": "...", "central_claim": "...", "key_insight": "...", "confidence": "high|medium|low", "ambiguity": ["..."]},
  "titles": [{"text": "...", "angle": "descriptive|insight|problem_solution|question|benefit|contrarian|story|search", "rationale": "...", "evidence_ids": ["..."], "limitations": ["..."]}],
  "selected_title_index": 0,
  "descriptions": {"short": "...", "long": "..."},
  "caption": "...",
  "hashtags": [{"tag": "#example", "category": "topic|niche|broad|trending", "reason": "..."}],
  "keywords": [{"phrase": "natural language search phrase", "reason": "..."}],
  "thumbnail_texts": ["..."],
  "cta": "..." or "",
  "content_angle": "...",
  "key_takeaway": "...",
  "suggested_audience": "...",
  "context_warnings": ["..."]
}
Omit a field only when the source cannot support it; do not fill it with invented text.
"""


def title_weights() -> dict[str, float]:
    """Return configured rubric weights, normalised to one."""
    weights = dict(DEFAULT_TITLE_WEIGHTS)
    raw = os.environ.get("CLIPFORGE_PACKAGING_TITLE_WEIGHTS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if key in weights:
                    try:
                        weights[key] = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        continue
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_TITLE_WEIGHTS)
    return {key: value / total for key, value in weights.items()}


def build_user_prompt(
    brief: ContentBrief,
    *,
    platform: str,
    settings: dict,
    research: KeywordResearchResult | None = None,
    title_angle: str | None = None,
    more_titles: int = 0,
) -> str:
    profile = packaging_profile(platform)
    payload = {
        "skill": {"id": SKILL_ID, "version": SKILL_VERSION, "prompt_version": PROMPT_VERSION},
        "platform": {
            "id": platform,
            "label": profile.get("label"),
            "limits": {
                "title_max": profile.get("title_max"),
                "title_recommended": profile.get("title_recommended"),
                "description_short_max": profile.get("description_short_max"),
                "description_long_max": profile.get("description_long_max"),
                "caption_max": profile.get("caption_max"),
                "caption_unit": profile.get("caption_hard_limit_unit"),
                "hashtag_max": profile.get("hashtag_max"),
                "hashtag_recommended": profile.get("hashtag_recommended"),
                "keyword_max": profile.get("keyword_max"),
                "thumbnail_text_max": profile.get("thumbnail_text_max"),
            },
            "format_notes": profile.get("notes") or "",
        },
        "creator_settings": {
            "tone": settings.get("tone"),
            "audience": settings.get("audience"),
            "language": settings.get("language"),
            "region": settings.get("region"),
            "niche": settings.get("niche"),
            "brand_voice": settings.get("brand_voice"),
            "hashtag_style": settings.get("hashtag_style"),
            "hashtag_count": settings.get("hashtag_count"),
            "cta_preference": settings.get("cta_preference"),
            "cta_text": settings.get("cta_text"),
            "description_style": settings.get("description_style"),
            "brand_terms": settings.get("brand_terms") or [],
            "restricted_words": settings.get("restricted_words") or [],
            "restricted_topics": settings.get("restricted_topics") or [],
            "restricted_claims": settings.get("restricted_claims") or [],
        },
        "content_brief": brief_context_payload(brief),
        "research": research_payload(research),
        "requested_angle": title_angle,
        "candidate_count": max(5, min(8, 5 + int(more_titles or 0))),
    }
    return json.dumps(payload, ensure_ascii=False)


def research_payload(research: KeywordResearchResult | None) -> dict:
    if research is None:
        return {"mode": "off", "note": "No external research was performed. Do not imply trend or search-volume data."}
    return {
        "mode": research.mode,
        "status": research.status,
        "retrieved_at": research.retrieved_at,
        "suggested_terms": research.suggested_terms[:12],
        "observations": [
            {
                "term": item.term,
                "provider": item.provider,
                "kind": item.kind,
                "value": item.value,
                "unit": item.unit,
                "region": item.region,
                "freshness": item.freshness,
                "limitations": item.limitations[:3],
                "manual": item.manual,
            }
            for item in research.observations[:20]
        ],
        "limitations": research.limitations[:6],
        "note": (
            "Use research to refine terminology only. Never invent metrics, and never force an unrelated "
            "trend into the title or description."
        ),
    }


def skill_manifest() -> dict:
    return {
        "id": SKILL_ID,
        "version": SKILL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "title_angles": list(TITLE_ANGLES),
        "rubric": title_weights(),
        "accuracy_gate": ACCURACY_GATE,
        "notes": [
            "Accuracy is a hard gate applied before ranking.",
            "Scores describe editorial fit, not predicted views, click-through rate or virality.",
            "Research is an enrichment layer; packaging works without it.",
        ],
    }
