"""The AI editorial director: one validated, evidence-grounded plan per project.

The director chooses *which* existing candidate moments become clips and *how*
they are cut. It never creates a moment, never invents a quote and never emits
an edit it cannot point at the source for. When no model is usable, a
deterministic planner produces the same document shape from the stored
candidate evidence, and says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from clipforge.config import Settings
from clipforge.db.repositories import Repositories
from clipforge.editorial.models import EditorialStrategy
from clipforge.editorial.strategy import STRATEGIES
from clipforge.errors import ProviderUnavailable
from clipforge.providers.llm.registry import LlmProviderRegistry
from clipforge.ranking.sentences import build_transcript_model

logger = logging.getLogger(__name__)

DIRECTOR_VERSION = 1
MIN_SEGMENT_MS = 1_200
MAX_CLIP_MS = 15 * 60 * 1000
HOOK_LEAD_MS = 10_000

SYSTEM_PROMPT = """You are the editorial director for a video clipping tool.
You receive candidate moments cut from one source video, each with its exact
source timestamps and transcript evidence. Choose which candidates to produce
and how each one is cut.

Hard rules:
- Select only the given candidate ids. Never invent a moment.
- Every segment must use source milliseconds inside that candidate's range
  (a hook may sit up to 10 seconds before the candidate start only when it is
  explicitly labelled role "hook" and its text is in the supplied transcript).
- Never write dialogue, reactions or events. Hook text must be copied from the
  transcript evidence.
- Different clips may use different strategies. Do not force hook-first.
- Preserve meaning: do not cut a sentence in the middle to hit a duration.
- Prefer coherence, context and payoff over a duration target.
- Return STRICT JSON only, matching the requested schema.

Allowed strategies: chronological, cold_open, in_medias_res, result_first,
question_answer, story_escalation, reaction_led, loop.
"""


class PlannedSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    role: Literal["hook", "context", "main", "development", "payoff", "loop"] = "main"

    @model_validator(mode="after")
    def _ordered(self) -> "PlannedSegment":
        if self.end_ms <= self.start_ms:
            raise ValueError("segment end must be after its start")
        return self

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class PlannedClip(BaseModel):
    candidate_id: str
    strategy: EditorialStrategy = "chronological"
    rationale: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    segments: list[PlannedSegment] = Field(min_length=1, max_length=20)
    hook: dict | None = None
    composition: Literal["auto", "crop", "fit", "pip", "stack", "facecam_focus"] = "auto"
    aspect: Literal["9:16", "16:9", "1:1"] | None = None
    caption_preset: str | None = None
    risks: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    deferred_reason: str | None = None

    @property
    def total_ms(self) -> int:
        return sum(segment.duration_ms for segment in self.segments)


class DirectorDecision(BaseModel):
    version: int = DIRECTOR_VERSION
    mode: Literal["model", "deterministic_fallback"] = "deterministic_fallback"
    provider: str = "none"
    model: str | None = None
    privacy: str = "none"
    selected: list[PlannedClip] = Field(default_factory=list)
    deferred: list[dict] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)
    requests_more_moments: bool = False
    notes: str = ""
    metrics: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self) -> "DirectorDecision":
        ids = [clip.candidate_id for clip in self.selected]
        if len(ids) != len(set(ids)):
            raise ValueError("a candidate may be selected only once")
        return self


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _evidence_text(segments: list[dict], start_ms: int, end_ms: int) -> str:
    words: list[str] = []
    for segment in segments:
        overlap = min(end_ms, int(segment.get("end_ms") or 0)) - max(
            start_ms, int(segment.get("start_ms") or 0)
        )
        if overlap <= 0:
            continue
        for word in segment.get("words") or []:
            word_start = int(word.get("start_ms") or 0)
            word_end = int(word.get("end_ms") or word_start)
            if word_end > start_ms and word_start < end_ms:
                words.append(str(word.get("word") or "").strip())
        if not segment.get("words"):
            words.append(str(segment.get("text") or "").strip())
    return " ".join(part for part in words if part)


def _sentence_rows(segments: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    rows: list[dict] = []
    for segment in segments:
        seg_start = int(segment.get("start_ms") or 0)
        seg_end = int(segment.get("end_ms") or 0)
        if seg_end <= start_ms or seg_start >= end_ms:
            continue
        rows.append(
            {
                "start_ms": seg_start,
                "end_ms": seg_end,
                "text": str(segment.get("text") or "").strip()[:400],
            }
        )
    return rows[:24]


def _content_hash(source: dict, transcript: dict | None) -> str:
    if source.get("content_hash"):
        return str(source["content_hash"])
    payload = [
        {
            "start_ms": segment.get("start_ms"),
            "end_ms": segment.get("end_ms"),
            "text": segment.get("text"),
        }
        for segment in (transcript or {}).get("segments", [])
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _candidate_document(candidate: dict, transcript_segments: list[dict]) -> dict:
    editorial = candidate.get("editorial") or {}
    decision = editorial.get("editorial") or {}
    timeline = editorial.get("timeline") or {}
    return {
        "id": candidate["id"],
        "start_ms": int(candidate["start_ms"]),
        "end_ms": int(candidate["end_ms"]),
        "title": candidate.get("title") or "",
        "summary": candidate.get("summary") or "",
        "excerpt": (candidate.get("excerpt") or "")[:1600],
        "moment_type": candidate.get("moment_type") or "",
        "speaker": candidate.get("speaker"),
        "score": round(float(candidate.get("overall") or 0), 1),
        "score_basis": "transcript features, not predicted reach",
        "hook": {
            "text": (candidate.get("hook_detail") or {}).get("text") or candidate.get("hook") or "",
            "start_ms": (candidate.get("hook_detail") or {}).get("start_ms"),
            "end_ms": (candidate.get("hook_detail") or {}).get("end_ms"),
        },
        "proposed_strategy": decision.get("strategy"),
        "proposed_rationale": decision.get("rationale"),
        "available_strategies": [
            key for key, profile in STRATEGIES.items() if profile.implemented
        ],
        "sentences": _sentence_rows(transcript_segments, candidate["start_ms"], candidate["end_ms"]),
        "existing_timeline_valid": bool(timeline.get("valid")),
        "risks": list(candidate.get("warnings") or [])[:8],
        "manually_edited": bool(candidate.get("edited")),
    }


def _cache_key(payload: dict, provider: dict) -> str:
    material = {
        "director": DIRECTOR_VERSION,
        "payload": payload,
        "provider": provider.get("provider"),
        "model": provider.get("model"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _validate_against_candidates(
    decision: DirectorDecision,
    candidates: dict[str, dict],
    transcript_segments: list[dict],
    *,
    source_duration_ms: int,
    limit: int,
) -> DirectorDecision:
    """Reject any plan that points outside the source evidence."""
    selected: list[PlannedClip] = []
    dropped: list[dict] = list(decision.rejected)
    for clip in decision.selected[:limit]:
        candidate = candidates.get(clip.candidate_id)
        if candidate is None:
            dropped.append({"candidate_id": clip.candidate_id, "reason": "unknown candidate id"})
            continue
        start = int(candidate["start_ms"])
        end = int(candidate["end_ms"])
        edited = bool(candidate.get("edited"))
        valid = True
        seen_spans: set[tuple[int, int]] = set()
        for segment in clip.segments:
            if segment.duration_ms < MIN_SEGMENT_MS:
                valid = False
                break
            if segment.end_ms > source_duration_ms:
                valid = False
                break
            if edited and (segment.start_ms < start or segment.end_ms > end):
                valid = False
                break
            allowed_start = start
            if segment.role == "hook" and not edited:
                allowed_start = max(0, start - HOOK_LEAD_MS)
            if segment.start_ms < allowed_start or segment.end_ms > end:
                valid = False
                break
            key = (segment.start_ms, segment.end_ms)
            if key in seen_spans:
                valid = False
                break
            seen_spans.add(key)
        total = sum(segment.duration_ms for segment in clip.segments)
        if not valid or total < 2_000 or total > MAX_CLIP_MS:
            dropped.append(
                {
                    "candidate_id": clip.candidate_id,
                    "reason": "segments fall outside the candidate's source evidence or are too short/long",
                }
            )
            continue
        if edited and any(segment.role != "main" for segment in clip.segments):
            dropped.append(
                {
                    "candidate_id": clip.candidate_id,
                    "reason": "a manually trimmed candidate keeps its chronological span",
                }
            )
            continue
        strategy = STRATEGIES.get(clip.strategy)
        if strategy is None or not strategy.implemented:
            dropped.append(
                {"candidate_id": clip.candidate_id, "reason": f"strategy {clip.strategy} is unavailable"}
            )
            continue

        hook = clip.hook
        if hook and hook.get("text"):
            hook_start = hook.get("start_ms")
            hook_end = hook.get("end_ms")
            grounded = False
            if isinstance(hook_start, int) and isinstance(hook_end, int) and hook_end > hook_start:
                spoken = _normalise(_evidence_text(transcript_segments, hook_start, hook_end))
                quoted = _normalise(str(hook.get("text")))
                grounded = bool(quoted) and (quoted in spoken or spoken in quoted)
            if not grounded:
                hook = None
                clip.risks.append("The proposed hook was not an exact transcript quote and was dropped.")
        clip = clip.model_copy(update={"hook": hook})
        selected.append(clip)

    return decision.model_copy(update={"selected": selected, "rejected": dropped[:20]})


def _fallback_clip(candidate: dict) -> PlannedClip:
    start = int(candidate["start_ms"])
    end = int(candidate["end_ms"])
    if candidate.get("edited"):
        return PlannedClip(
            candidate_id=candidate["id"],
            strategy="chronological",
            rationale="Manual trim is preserved; the clip plays in source order.",
            segments=[PlannedSegment(start_ms=start, end_ms=end, role="main")],
            risks=list(candidate.get("warnings") or [])[:6],
            evidence=[f"candidate:{candidate['id']}", f"source:{start}-{end}"],
        )

    editorial = candidate.get("editorial") or {}
    decision = editorial.get("editorial") or {}
    timeline = editorial.get("timeline") or {}
    strategy = decision.get("strategy") or "chronological"
    if strategy not in STRATEGIES or not STRATEGIES[strategy].implemented:
        strategy = "chronological"
    segments: list[PlannedSegment] = []
    if timeline.get("valid") and timeline.get("entries"):
        for entry in timeline["entries"][:20]:
            try:
                segment = PlannedSegment(
                    start_ms=int(entry["source_start_ms"]),
                    end_ms=int(entry["source_end_ms"]),
                    role=entry.get("role", "main"),
                )
            except (KeyError, ValueError, TypeError):
                segments = []
                break
            segments.append(segment)
    if not segments:
        segments = [PlannedSegment(start_ms=start, end_ms=end, role="main")]
        strategy = "chronological" if not decision.get("strategy") else strategy
    rationale = decision.get("rationale") or (
        "Deterministic selection: the moment stands alone and keeps its source order."
    )
    return PlannedClip(
        candidate_id=candidate["id"],
        strategy=strategy,
        rationale=rationale,
        segments=segments,
        hook=(candidate.get("hook_detail") or None),
        composition="auto",
        risks=list(candidate.get("warnings") or [])[:6]
        + list((editorial.get("timeline") or {}).get("warnings") or [])[:4],
        evidence=[
            f"candidate:{candidate['id']}",
            f"source:{start}-{end}",
            f"strategy:{strategy}",
        ],
    )


def _deterministic_decision(
    candidates: list[dict],
    *,
    limit: int,
    reason: str,
    provider: dict,
    deferred_reason: str,
) -> DirectorDecision:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.get("decision") != "rejected"
    ]
    eligible.sort(key=lambda item: float(item.get("overall") or 0), reverse=True)
    selected = [_fallback_clip(candidate) for candidate in eligible[:limit]]
    deferred = [
        {
            "candidate_id": candidate["id"],
            "reason": deferred_reason,
        }
        for candidate in eligible[limit:]
    ]
    return DirectorDecision(
        mode="deterministic_fallback",
        provider=str(provider.get("provider") or "none"),
        model=provider.get("model"),
        privacy=str(provider.get("privacy") or "none"),
        selected=selected,
        deferred=deferred[:40],
        requests_more_moments=len(eligible) < limit,
        notes=reason,
        metrics={"model_calls": 0},
    )


def _model_prompt(payload: dict) -> str:
    return (
        "Choose the clips to produce from the candidate list. Return JSON with keys: "
        '{"selected":[{"candidate_id":"...","strategy":"chronological",'
        '"rationale":"...","confidence":0.0,"segments":[{"start_ms":0,"end_ms":1000,"role":"main"}],'
        '"hook":{"text":"exact quote","start_ms":0,"end_ms":1000},'
        '"composition":"auto","aspect":"9:16","caption_preset":null,'
        '"risks":["..."],"evidence":["candidate:id","source:0-1000"]}],'
        '"deferred":[{"candidate_id":"...","reason":"..."}],'
        '"rejected":[{"candidate_id":"...","reason":"..."}],'
        '"requests_more_moments":false,"notes":"..."}'
        "\n\nRequest:\n" + json.dumps(payload, ensure_ascii=False)
    )


def _model_decision(
    adapter,
    payload: dict,
    *,
    candidates: dict[str, dict],
    transcript_segments: list[dict],
    source_duration_ms: int,
    limit: int,
    provider: dict,
) -> DirectorDecision:
    system = SYSTEM_PROMPT
    user = _model_prompt(payload)
    try:
        parsed, meta = adapter.complete_json_meta(system, user, max_tokens=4_000)
        decision = DirectorDecision.model_validate(
            {
                **parsed,
                "mode": "model",
                "provider": provider.get("provider"),
                "model": provider.get("model"),
                "privacy": provider.get("privacy"),
            }
        )
        decision = _validate_against_candidates(
            decision,
            candidates,
            transcript_segments,
            source_duration_ms=source_duration_ms,
            limit=limit,
        )
        if not decision.selected:
            raise ValueError("the model selected no valid candidate")
        return decision.model_copy(update={"metrics": meta})
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        # One targeted retry that names the defect is allowed. A second failure
        # falls back rather than looping or weakening validation.
        correction = (
            "Your previous answer was rejected: "
            + str(exc)[:500]
            + "\nReturn the same JSON schema again with only valid candidate ids and "
            "source spans inside each candidate. Use an exact transcript quote or omit the hook."
        )
        parsed, meta = adapter.complete_json_meta(system, user + correction, max_tokens=4_000)
        decision = DirectorDecision.model_validate(
            {
                **parsed,
                "mode": "model",
                "provider": provider.get("provider"),
                "model": provider.get("model"),
                "privacy": provider.get("privacy"),
            }
        )
        decision = _validate_against_candidates(
            decision,
            candidates,
            transcript_segments,
            source_duration_ms=source_duration_ms,
            limit=limit,
        )
        if not decision.selected:
            raise ProviderUnavailable(
                "The model returned no grounded selection", code="llm_ungrounded"
            )
        meta = {**meta, "validation_retry": True}
        return decision.model_copy(update={"metrics": meta})


def build_director_payload(
    *,
    project: dict,
    source: dict,
    candidates: list[dict],
    transcript: dict | None,
    settings: dict,
    request: dict,
) -> dict:
    segments = (transcript or {}).get("segments") or []
    speakers = sorted(
        {
            str(segment.get("speaker"))
            for segment in segments
            if segment.get("speaker")
        }
    )
    return {
        "project": {"name": project.get("name"), "id": project.get("id")},
        "source": {
            "duration_ms": int(source.get("duration_ms") or 0),
            "has_video": bool(source.get("has_video")),
            "has_audio": bool(source.get("has_audio")),
            "language": (transcript or {}).get("language"),
            "speakers_available": bool(speakers),
        },
        "targets": {
            "clip_count": int(request.get("clip_count") or 5),
            "platform": request.get("platform") or "portrait_short",
            "tone": request.get("tone") or "match the source",
            "duration_preference_ms": request.get("duration_preference_ms"),
        },
        "profile": {
            "captions": (settings.get("captions") or {}),
            "export": (settings.get("export") or {}),
            "brief": (settings.get("brief") or {}),
            "rules": (settings.get("rules") or {}),
        },
        "capabilities": {
            "composition_modes": ["auto", "crop", "fit", "pip", "stack", "facecam_focus"],
            "native_video_analysis": True,
            "speaker_diarisation": bool(speakers),
        },
        "candidates": [
            _candidate_document(candidate, segments) for candidate in candidates[: settings.get("max_candidates", 24)]
        ],
    }


def run_director(
    repos: Repositories,
    settings: Settings,
    registry: LlmProviderRegistry | None,
    *,
    project: dict,
    source: dict,
    candidates: list[dict],
    transcript: dict | None,
    resolved_settings: dict,
    request: dict,
) -> dict:
    """Return one decision document. Never raises for a provider failure."""
    limit = max(1, min(int(request.get("clip_count") or 5), settings.autoproduce_clip_limit))
    payload = build_director_payload(
        project=project,
        source=source,
        candidates=candidates,
        transcript=transcript,
        settings={**resolved_settings, "max_candidates": 24},
        request={**request, "clip_count": limit},
    )
    transcript_segments = (transcript or {}).get("segments") or []
    by_id = {candidate["id"]: candidate for candidate in candidates}
    registry = registry or LlmProviderRegistry(settings)
    selection = registry.select()
    provider = dict(selection.info)
    provider["available"] = selection.available
    remote_blocked = (
        selection.usable
        and provider.get("privacy") == "remote"
        and not request.get("editorial_consent", False)
    )

    if not selection.usable or not settings.llm_editorial_enabled or remote_blocked:
        if remote_blocked:
            reason = (
                "Remote editorial planning needs explicit consent for this request; "
                "deterministic selection was used."
            )
        elif selection.reason and not selection.usable:
            reason = selection.reason
        else:
            reason = "Model editorial planning is disabled; deterministic selection was used."
        decision = _deterministic_decision(
            candidates,
            limit=limit,
            reason=reason,
            provider=provider,
            deferred_reason="below the requested clip count in the deterministic ranking",
        )
        return decision.model_dump()

    content_hash = _content_hash(source, transcript)
    key = _cache_key(payload, provider)
    cached = repos.director_cache.get(key)
    if cached and cached.get("content_hash") == content_hash:
        result = dict(cached["result"])
        result["metrics"] = {**(result.get("metrics") or {}), "cache": "hit"}
        return result

    try:
        assert selection.adapter is not None
        decision = _model_decision(
            selection.adapter,
            payload,
            candidates=by_id,
            transcript_segments=transcript_segments,
            source_duration_ms=int(source.get("duration_ms") or 0),
            limit=limit,
            provider=provider,
        )
    except Exception as exc:
        logger.warning("Editorial model unavailable, using deterministic plan: %s", exc)
        decision = _deterministic_decision(
            candidates,
            limit=limit,
            reason=(
                "The configured model could not produce a grounded plan "
                f"({type(exc).__name__}); deterministic selection was used."
            ),
            provider=provider,
            deferred_reason="the model did not select this candidate",
        )
        decision = decision.model_copy(update={"metrics": {"fallback": True}})
        return decision.model_dump()

    result = decision.model_dump()
    repos.director_cache.put(
        key,
        provider=str(provider.get("provider") or "none"),
        model=str(provider.get("model") or ""),
        content_hash=content_hash,
        result=result,
    )
    return result


def director_document(decision: dict) -> dict:
    """A compact, honest summary for the project UI."""
    return {
        "version": decision.get("version"),
        "mode": decision.get("mode"),
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "privacy": decision.get("privacy"),
        "selected": len(decision.get("selected") or []),
        "deferred": len(decision.get("deferred") or []),
        "rejected": len(decision.get("rejected") or []),
        "requests_more_moments": bool(decision.get("requests_more_moments")),
        "notes": decision.get("notes") or "",
        "metrics": decision.get("metrics") or {},
        "strategies": sorted(
            {clip.get("strategy") for clip in decision.get("selected") or [] if clip.get("strategy")}
        ),
    }


def decision_evidence(decision: dict) -> dict:
    return {
        clip["candidate_id"]: {
            "strategy": clip.get("strategy"),
            "rationale": clip.get("rationale"),
            "confidence": clip.get("confidence"),
            "segments": clip.get("segments"),
            "evidence": clip.get("evidence"),
            "risks": clip.get("risks"),
        }
        for clip in decision.get("selected") or []
    }


def decision_by_candidate(decision: dict) -> dict[str, dict]:
    return {clip["candidate_id"]: clip for clip in decision.get("selected") or []}


def strategy_label(strategy: str) -> str:
    profile = STRATEGIES.get(strategy)
    return profile.label if profile else "Chronological"


def transcript_context(segments: list[dict], start_ms: int, end_ms: int) -> list[dict]:
    model = build_transcript_model(segments, max(end_ms, 1))
    rows: list[dict] = []
    for sentence in model.sentences:
        if sentence.end_ms <= start_ms or sentence.start_ms >= end_ms:
            continue
        rows.append(
            {
                "start_ms": sentence.start_ms,
                "end_ms": sentence.end_ms,
                "text": sentence.text[:400],
            }
        )
    return rows[:20]


def engine_descriptor(decision: dict) -> dict[str, Any]:
    """What produced this plan, for the manifest and the review report."""
    return {
        "director_version": DIRECTOR_VERSION,
        "mode": decision.get("mode"),
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "privacy": decision.get("privacy"),
        "validation": "schema + source-span + transcript-quote checks",
    }
