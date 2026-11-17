"""Retention structure for one chosen moment.

The plan partitions the moment into contiguous, non-overlapping source spans and
gives each one a narrative job. It does not invent dialogue or duplicate footage:
every span comes from the transcript, and every boundary is a sentence boundary
or the user's own cut. Render segments stay independent; the plan is stored on
the spec and in the EDL so both preview and export can explain the same edit.
"""

from __future__ import annotations

from clipforge.briefs.spec import EditorialPolicy, HookPolicy, PacingPolicy
from clipforge.editorial.models import (
    HookCandidateDocument,
    NarrativeRelationship,
    RetentionPlan,
    RetentionSegment,
)
from clipforge.editorial.pacing import resolve_pacing
from clipforge.editorial.strategy import plan_editorial
from clipforge.ranking.lexicon import ANAPHORA_STARTS, CONTRASTIVE_STARTS, STOPWORDS
from clipforge.ranking.sentences import Sentence, TranscriptModel, tokenize

CONTEXT_WINDOW = 0.22
MIN_ROLE_MS = 1_200


def _sentences_in(model: TranscriptModel, start_ms: int, end_ms: int) -> list[Sentence]:
    return [
        sentence
        for sentence in model.sentences
        if sentence.end_ms > start_ms and sentence.start_ms < end_ms
    ]


def _needs_context(sentence: Sentence) -> bool:
    text = sentence.text.strip()
    lowered = text.lower()
    if any(lowered.startswith(marker) for marker in ANAPHORA_STARTS):
        return True
    if any(lowered.startswith(marker) for marker in CONTRASTIVE_STARTS):
        return True
    tokens = tokenize(text)
    pronouns = sum(1 for token in tokens if token in {"it", "this", "that", "they", "them", "he", "she"})
    if tokens and pronouns / len(tokens) >= 0.28:
        return True
    # A four-word opener without a subject is usually a fragment of the prior thought.
    return len(tokens) <= 4 and not any(token in {"i", "we", "you", "they", "he", "she"} for token in tokens)


def _point_score(sentence: Sentence) -> float:
    tokens = tokenize(sentence.text)
    if not tokens:
        return 0.0
    content = [token for token in tokens if token not in STOPWORDS]
    score = min(1.0, len(content) / 12)
    if any(character.isdigit() for character in sentence.text):
        score += 0.25
    if any(marker in sentence.text.lower() for marker in ("because", "turns out", "result", "actually")):
        score += 0.15
    if 4 <= len(tokens) <= 16:
        score += 0.15
    if sentence.ends_with_terminator:
        score += 0.10
    return score


def _role_sequence(count: int, first_needs_context: bool, duration_preset: str) -> list[str]:
    """Allocate narrative jobs by the selected duration structure.

    Short is one idea, a hook and a payoff. Standard adds setup and a main beat.
    Extended adds development. Roles are metadata over source order; they do not
    force a reorder on their own.
    """
    if count <= 0:
        return []
    if count == 1:
        return ["main"]
    if count == 2:
        return ["main", "payoff"]
    if duration_preset == "short":
        return ["main"] * (count - 1) + ["payoff"]
    if duration_preset == "standard":
        if first_needs_context:
            return ["context"] + ["main"] * (count - 2) + ["payoff"]
        return ["main"] * (count - 1) + ["payoff"]
    if count == 3:
        return ["context", "main", "payoff"] if first_needs_context else ["main", "development", "payoff"]
    if first_needs_context:
        roles = ["context", "main"] + ["development"] * (count - 3) + ["payoff"]
    else:
        roles = ["main"] + ["development"] * (count - 2) + ["payoff"]
    return roles


def _group_sentences(sentences: list[Sentence], roles: list[str]) -> list[tuple[str, list[Sentence]]]:
    groups: list[tuple[str, list[Sentence]]] = []
    for position, sentence in enumerate(sentences):
        role = roles[position]
        if groups and groups[-1][0] == role:
            groups[-1][1].append(sentence)
        else:
            groups.append((role, [sentence]))
    return groups


def _context_loss(sentences: list[Sentence]) -> tuple[float, list[str]]:
    if not sentences:
        return 0.0, []
    first = sentences[0]
    lowered = first.text.lower()
    tokens = tokenize(first.text)
    score = 0.08
    warnings: list[str] = []
    if any(lowered.startswith(marker) for marker in ANAPHORA_STARTS):
        score += 0.55
        warnings.append("The opening leans on a word said earlier in the source.")
    if any(lowered.startswith(marker) for marker in CONTRASTIVE_STARTS):
        score += 0.30
        warnings.append("The opening argues against a setup that is not in the clip.")
    pronouns = sum(1 for token in tokens if token in {"it", "this", "that", "they", "them", "he", "she"})
    if tokens and pronouns / len(tokens) >= 0.25:
        score += 0.15
        warnings.append("The opening uses several pronouns before naming their subject.")
    if len(tokens) <= 4 and not any(token in {"i", "we", "you"} for token in tokens):
        score += 0.18
        warnings.append("The opening is very short and may begin mid-thought.")
    return min(1.0, score), warnings


def _segment_confidence(sentences: list[Sentence], role: str, group_duration: int) -> float:
    if not sentences:
        return 0.3
    score = 0.62
    if all(sentence.ends_with_terminator for sentence in sentences):
        score += 0.12
    if group_duration >= MIN_ROLE_MS:
        score += 0.10
    if role in {"main", "payoff"}:
        score += 0.05
    if role == "development" and len(sentences) > 1:
        score += 0.03
    if not sentences[-1].ends_with_terminator:
        score -= 0.18
    if group_duration < 800:
        score -= 0.25
    return max(0.1, min(0.97, score))


def _relationships(segments: list[RetentionSegment]) -> list[NarrativeRelationship]:
    roles = [segment.role for segment in segments]
    relationships: list[NarrativeRelationship] = []
    if "context" in roles and "main" in roles:
        relationships.append(NarrativeRelationship(source_role="context", target_role="main", relation="sets_up"))
    if "development" in roles and "main" in roles:
        relationships.append(
            NarrativeRelationship(source_role="development", target_role="main", relation="elaborates")
        )
    if "payoff" in roles and "main" in roles:
        relationships.append(NarrativeRelationship(source_role="payoff", target_role="main", relation="resolves"))
    return relationships


def build_retention_plan(
    model: TranscriptModel,
    *,
    start_ms: int,
    end_ms: int,
    excerpt: str = "",
    pacing: PacingPolicy | dict | None = None,
    hook_policy: HookPolicy | None = None,
    hook: dict | None = None,
    hook_candidates: list[dict] | None = None,
    editorial: EditorialPolicy | dict | None = None,
    duration_preset: str = "extended",
) -> RetentionPlan:
    start_ms = max(0, int(start_ms))
    end_ms = max(start_ms + 1, int(end_ms))
    settings = resolve_pacing(pacing)
    sentences = _sentences_in(model, start_ms, end_ms)
    warnings: list[str] = []
    review_required = False

    hook_document = None
    candidate_documents: list[HookCandidateDocument] = []
    for raw in hook_candidates or ([hook] if hook else []):
        if not raw:
            continue
        try:
            candidate_documents.append(HookCandidateDocument.model_validate(raw))
        except ValueError:
            continue
    if hook:
        try:
            hook_document = HookCandidateDocument.model_validate(hook)
        except ValueError:
            hook_document = None
    if hook_policy and hook_policy.required and not hook_document:
        warnings.append("This brief requires a hook, but no grounded hook fitted the moment.")
        review_required = True

    if not sentences:
        return RetentionPlan(
            profile=settings.profile,
            segments=[
                RetentionSegment(
                    role="main",
                    source_start_ms=start_ms,
                    source_end_ms=end_ms,
                    key_point=excerpt[:220],
                    reason="No transcript sentence aligned with this span; the user's cut is preserved.",
                    confidence=0.35,
                    review=True,
                )
            ],
            confidence=0.35,
            review_required=True,
            warnings=["No transcript covered this moment; retention structure could not be verified."],
            hook=hook_document,
            candidates=candidate_documents,
        )

    first_needs_context = _needs_context(sentences[0])
    roles = _role_sequence(len(sentences), first_needs_context, duration_preset)
    groups = _group_sentences(sentences, roles)
    plan_segments: list[RetentionSegment] = []
    previous_role: str | None = None
    for index, (role, group) in enumerate(groups):
        group_start = max(start_ms, group[0].start_ms if index else start_ms)
        group_end = min(end_ms, group[-1].end_ms if index == len(groups) - 1 else group[-1].end_ms)
        if group_end <= group_start:
            group_start, group_end = max(start_ms, group[0].start_ms), min(end_ms, group[-1].end_ms)
        key_sentence = max(group, key=_point_score)
        context_score, context_warnings = _context_loss(group) if index == 0 else (0.05, [])
        confidence = _segment_confidence(group, role, group_end - group_start)
        if context_score >= 0.45:
            review_required = True
            warnings.extend(context_warnings)
        if confidence < 0.5:
            review_required = True
            warnings.append(f"The {role} beat is short or ends mid-thought; review its boundary.")
        pattern_interrupt = bool(
            settings.pattern_interrupts
            and previous_role is not None
            and previous_role != role
            and (group_end - group_start) >= MIN_ROLE_MS
        )
        plan_segments.append(
            RetentionSegment(
                role=role,
                source_start_ms=int(group_start),
                source_end_ms=int(group_end),
                sentence_start=group[0].index,
                sentence_end=group[-1].index,
                key_point=" ".join(key_sentence.text.split())[:220],
                reason=(
                    f"{role.capitalize()} beat: {len(group)} sentence(s), "
                    f"{((group_end - group_start) / 1000):.1f}s."
                ),
                confidence=round(confidence, 3),
                context_loss=round(context_score, 3),
                transition="continuous" if index == 0 else "cut",
                review=context_score >= 0.45 or confidence < 0.5,
                pattern_interrupt=pattern_interrupt,
            )
        )
        previous_role = role

    relationships = _relationships(plan_segments)
    if hook_document:
        first_role = plan_segments[0].role if plan_segments else "main"
        relationships.insert(
            0,
            NarrativeRelationship(source_role="hook", target_role=first_role, relation="sets_up"),
        )
    confidence = sum(segment.confidence for segment in plan_segments) / max(1, len(plan_segments))
    context_loss = max((segment.context_loss for segment in plan_segments), default=0.0)
    if not any(segment.role == "payoff" for segment in plan_segments):
        review_required = True
        warnings.append("The plan has no payoff beat; the ending may feel unresolved.")
    warnings = list(dict.fromkeys(warnings))
    plan = RetentionPlan(
        profile=settings.profile,
        segments=plan_segments,
        relationships=relationships,
        confidence=round(confidence, 3),
        context_loss=round(context_loss, 3),
        review_required=review_required,
        warnings=warnings,
        hook=hook_document,
        candidates=candidate_documents,
    )
    plan = plan_editorial(plan, model, policy=editorial, duration_preset=duration_preset)
    if plan.timeline is not None:
        # Mirror output coordinates onto the narrative beats so the preview and
        # the renderer cannot disagree about when a role plays.
        for entry in plan.timeline.entries:
            if entry.role == "hook":
                continue
            for segment in plan.segments:
                if (
                    segment.source_start_ms == entry.source_start_ms
                    and segment.source_end_ms == entry.source_end_ms
                ):
                    segment.output_start_ms = entry.output_start_ms
                    segment.output_end_ms = entry.output_end_ms
                    segment.transition = entry.transition
                    break
    return plan
