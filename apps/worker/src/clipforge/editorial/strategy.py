"""Explainable editorial strategy selection.

The planner only offers edits the source can actually support. A strategy that
would need reaction shots, a second camera or rendered loop points is reported as
unavailable instead of being applied and described as if it had happened.

Reordering is expressed as disjoint source spans in a new order. The renderer
already builds one output timeline from those spans, so captions, audio and video
are recalculated from the same source times. Overlap and missing speech are
checked here before a plan is allowed to become a render.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge.briefs.spec import GENRE_GUIDANCE, EditorialPolicy
from clipforge.editorial.models import (
    EditorialAlternative,
    EditorialDecision,
    EditorialStrategy,
    RetentionPlan,
    TimelineEntry,
    TimelineReport,
)
from clipforge.ranking.lexicon import QUESTION_WORDS
from clipforge.ranking.sentences import Sentence, TranscriptModel

REACTION_STARTS = ("oh", "wow", "whoa", "no way", "wait", "look", "here's", "heres", "this is")
MIN_MOVE_MS = 4_000
MIN_LOOP_MS = 1_200
OVERLAP_TOLERANCE_MS = 40
COVERAGE_TOLERANCE_MS = 120


@dataclass(frozen=True)
class StrategyProfile:
    id: EditorialStrategy
    label: str
    description: str
    reorders: bool = False
    needs_hook: bool = False
    implemented: bool = True


STRATEGIES: dict[str, StrategyProfile] = {
    "chronological": StrategyProfile(
        "chronological",
        "Chronological",
        "Play the moment in source order. Always available as the baseline.",
    ),
    "cold_open": StrategyProfile(
        "cold_open",
        "Cold open → flashback → payoff",
        "Open on a grounded later line, flash back to the setup, then land the payoff.",
        reorders=True,
        needs_hook=True,
    ),
    "in_medias_res": StrategyProfile(
        "in_medias_res",
        "In medias res",
        "Start inside the action, then return to the context that explains it.",
        reorders=True,
    ),
    "result_first": StrategyProfile(
        "result_first",
        "Result first",
        "Lead with the outcome, then show how the moment arrives there.",
        reorders=True,
    ),
    "question_answer": StrategyProfile(
        "question_answer",
        "Question → answer → evidence → conclusion",
        "Keep source order because the moment already asks and answers the question.",
    ),
    "story_escalation": StrategyProfile(
        "story_escalation",
        "Escalation → climax",
        "Keep source order and label the rising beats through to the payoff.",
    ),
    "reaction_led": StrategyProfile(
        "reaction_led",
        "Reaction led",
        "Keep source order because the moment already opens on the reaction.",
    ),
    "montage": StrategyProfile(
        "montage",
        "Montage / jump cuts",
        "Needs shot-level cutaways that a single continuous source does not contain.",
        implemented=False,
    ),
    "loop": StrategyProfile(
        "loop",
        "Payoff → callback → loop",
        "Replays the opening beat at the end so the clip can start again without a hard cut.",
        reorders=True,
    ),
    "open_loop": StrategyProfile(
        "open_loop",
        "Open loop explanation",
        "Needs a separate unanswered setup elsewhere in the source.",
        implemented=False,
    ),
}


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def _main_duration(plan: RetentionPlan) -> int:
    return sum(max(0, segment.source_end_ms - segment.source_start_ms) for segment in plan.segments)


def _has_role(plan: RetentionPlan, role: str) -> bool:
    return any(segment.role == role for segment in plan.segments)


def _payoff_index(plan: RetentionPlan) -> int | None:
    for index in range(len(plan.segments) - 1, -1, -1):
        if plan.segments[index].role in {"payoff", "main"}:
            return index
    return None


def _movable_index(plan: RetentionPlan, roles: tuple[str, ...]) -> int | None:
    """Last confident beat that is long enough to lead with, but not the opening."""
    for index in range(len(plan.segments) - 1, 0, -1):
        segment = plan.segments[index]
        if segment.role in roles and segment.duration_ms >= MIN_MOVE_MS:
            return index
    return None


def _hook_document(plan: RetentionPlan):
    hook = plan.hook
    if hook is None or hook.source == "none" or not hook.approved:
        return None
    if hook.duration_ms <= 0 or hook.start_ms >= hook.end_ms:
        return None
    return hook


def _hook_external_after(plan: RetentionPlan):
    hook = _hook_document(plan)
    if hook is not None and hook.direction == "after":
        return hook
    return None


def _first_sentence(plan: RetentionPlan, model: TranscriptModel) -> Sentence | None:
    if not plan.segments:
        return None
    index = plan.segments[0].sentence_start
    if index is None:
        return None
    return next((sentence for sentence in model.sentences if sentence.index == index), None)


def _is_question(sentence: Sentence | None) -> bool:
    if sentence is None:
        return False
    lowered = " ".join(sentence.text.lower().split())
    words = lowered.split()
    return lowered.endswith("?") or bool(words and words[0] in QUESTION_WORDS)


def _is_reaction(sentence: Sentence | None) -> bool:
    if sentence is None:
        return False
    lowered = " ".join(sentence.text.lower().split())
    return any(lowered.startswith(start) for start in REACTION_STARTS)


def _has_setup(plan: RetentionPlan) -> bool:
    return _has_role(plan, "context") or _has_role(plan, "development")


def _plan_order(plan: RetentionPlan) -> list[int]:
    return list(range(len(plan.segments)))


def _strategy_order(plan: RetentionPlan, strategy: str) -> list[int]:
    if strategy in {"result_first", "in_medias_res"}:
        target = _payoff_index(plan) if strategy == "result_first" else _movable_index(
            plan, ("main", "development", "payoff")
        )
        if target is not None and target > 0:
            return [target] + [index for index in range(len(plan.segments)) if index != target]
    return _plan_order(plan)


def _cold_open_available(plan: RetentionPlan, policy: EditorialPolicy) -> tuple[bool, str]:
    hook = _hook_external_after(plan)
    if hook is None:
        return False, "No grounded later line was strong enough to open on."
    if policy.flashback == "avoid":
        return False, "Flashbacks are disabled for this brief."
    if policy.flashback == "manual" and policy.preferred_strategy != "cold_open":
        return False, "Flashbacks are manual; choose the cold open explicitly."
    if not _has_role(plan, "payoff"):
        return False, "The moment does not resolve to a payoff, so a cold open would promise nothing."
    if not _has_setup(plan):
        return False, "There is no setup beat to flash back to after the opening line."
    if hook.quality < 55:
        return False, "The available opening line is not strong enough to justify reordering."
    return True, "A grounded later line can open the clip and the setup resolves it."


def _loop_span(plan: RetentionPlan) -> tuple[int, int] | None:
    """The opening beat that a loop replays, or None when it is too short."""
    if not plan.segments:
        return None
    opening = plan.segments[0]
    if opening.duration_ms < MIN_LOOP_MS:
        return None
    return opening.source_start_ms, opening.source_end_ms


def _loop_available(plan: RetentionPlan, policy: EditorialPolicy) -> tuple[bool, str]:
    if policy.loop_ending == "disabled":
        return False, "Loop endings are disabled for this brief."
    if policy.loop_ending == "manual" and policy.preferred_strategy != "loop":
        return False, "Loop endings are manual; choose the loop strategy explicitly."
    if _loop_span(plan) is None:
        return False, "The opening beat is shorter than 1.2s, so replaying it would read as a stutter."
    return True, "The opening beat is long enough to replay once as a deliberate loop."


def _build_alternative(
    plan: RetentionPlan,
    strategy: str,
    *,
    available: bool,
    reason: str,
    pacing_profile: str | None = None,
) -> EditorialAlternative:
    profile = STRATEGIES.get(strategy, STRATEGIES["chronological"])
    return EditorialAlternative(
        id=strategy if pacing_profile is None else f"{strategy}:{pacing_profile}",
        label=profile.label if pacing_profile is None else f"{profile.label} · {pacing_profile} pacing",
        strategy=profile.id,
        description=profile.description,
        available=available,
        confidence=round(plan.confidence, 3),
        estimated_duration_ms=_main_duration(plan),
        pacing_profile=pacing_profile or plan.profile,
        reason=reason,
        requires_approval=profile.reorders,
    )


def _alternatives(plan: RetentionPlan, model: TranscriptModel, policy: EditorialPolicy) -> list[EditorialAlternative]:
    cold_ok, cold_reason = _cold_open_available(plan, policy)
    loop_ok, loop_reason = _loop_available(plan, policy)
    payoff = _payoff_index(plan)
    hot = _movable_index(plan, ("main", "development", "payoff"))
    first = _first_sentence(plan, model)
    alternatives = [
        _build_alternative(
            plan,
            "chronological",
            available=True,
            reason="The baseline: every source span stays in order and no speech moves.",
        ),
        _build_alternative(plan, "cold_open", available=cold_ok, reason=cold_reason),
        _build_alternative(
            plan,
            "result_first",
            available=payoff is not None and payoff > 0 and _has_setup(plan),
            reason=(
                "The payoff can lead, then the clip returns to the setup."
                if payoff is not None and payoff > 0
                else "No later payoff beat can be moved to the front."
            ),
        ),
        _build_alternative(
            plan,
            "in_medias_res",
            available=hot is not None,
            reason=(
                "A confident later beat can start the clip before the context."
                if hot is not None
                else "No later beat is long enough to start inside the action."
            ),
        ),
        _build_alternative(
            plan,
            "question_answer",
            available=_is_question(first) and _has_role(plan, "payoff"),
            reason=(
                "The moment already opens with a question and resolves it."
                if _is_question(first)
                else "The moment does not open with a question."
            ),
        ),
        _build_alternative(
            plan,
            "story_escalation",
            available=len(plan.segments) >= 3 and _has_role(plan, "payoff"),
            reason="Source order already escalates through the development beats."
            if len(plan.segments) >= 3
            else "Too few beats to show an escalation.",
        ),
        _build_alternative(
            plan,
            "reaction_led",
            available=_is_reaction(first),
            reason="The moment opens on a reaction." if _is_reaction(first) else "No reaction opens the moment.",
        ),
        _build_alternative(
            plan,
            "montage",
            available=False,
            reason="This source has no shot-level coverage to cut into a montage.",
        ),
        _build_alternative(
            plan,
            "loop",
            available=loop_ok,
            reason=loop_reason,
        ),
        _build_alternative(
            plan,
            "open_loop",
            available=False,
            reason="No separate unanswered setup exists elsewhere in the source.",
        ),
        _build_alternative(
            plan,
            "chronological",
            available=True,
            reason="Same source order with the faster pacing profile; review the removed pauses.",
            pacing_profile="fast",
        ),
        _build_alternative(
            plan,
            "chronological",
            available=True,
            reason="Same source order with the calmer conversational pacing profile.",
            pacing_profile="conversational",
        ),
    ]
    return alternatives


def _base_score_strategy(
    strategy: str,
    plan: RetentionPlan,
    policy: EditorialPolicy,
    *,
    cold_reason: str,
    cold_ok: bool,
) -> tuple[float, list[str]]:
    hook = _hook_document(plan)
    concerns: list[str] = []
    if strategy == "chronological":
        score = 0.66 + plan.confidence * 0.18
        if plan.context_loss > 0.4:
            score -= 0.08
        return _clamp(score), concerns
    if strategy == "cold_open":
        score = 0.50 + plan.confidence * 0.18
        if hook is not None:
            score += min(0.22, hook.quality / 100 * 0.28)
        if _has_role(plan, "payoff"):
            score += 0.06
        # Requirement: a qualified cold open is the preferred default.
        score += 0.12
        if not cold_ok:
            concerns.append(cold_reason)
        if plan.context_loss > 0.35:
            concerns.append("The hook is followed by a setup that already leans on earlier context.")
            score -= 0.10
        return _clamp(score), concerns
    if strategy == "result_first":
        score = 0.56 + plan.confidence * 0.12
        concerns.append("Leading with the payoff can make the later setup feel like a flashback.")
        return _clamp(score), concerns
    if strategy == "in_medias_res":
        score = 0.57 + plan.confidence * 0.12
        concerns.append("Starting mid-action may ask the viewer to wait for context.")
        return _clamp(score), concerns
    if strategy == "question_answer":
        return _clamp(0.67 + plan.confidence * 0.16), concerns
    if strategy == "story_escalation":
        return _clamp(0.64 + plan.confidence * 0.16), concerns
    if strategy == "reaction_led":
        return _clamp(0.62 + plan.confidence * 0.16), concerns
    return 0.5, concerns


def _genre_bonus(policy: EditorialPolicy, strategy: str) -> float:
    guide = GENRE_GUIDANCE.get(policy.genre)
    if not guide:
        return 0.0
    preferred = guide.get("preferred") or ()
    if strategy not in preferred:
        return 0.0
    position = preferred.index(strategy)
    return max(0.02, 0.07 - position * 0.02)


def _score_strategy(
    strategy: str,
    plan: RetentionPlan,
    policy: EditorialPolicy,
    *,
    cold_reason: str,
    cold_ok: bool,
) -> tuple[float, list[str]]:
    score, concerns = _base_score_strategy(
        strategy, plan, policy, cold_reason=cold_reason, cold_ok=cold_ok
    )
    return _clamp(score + _genre_bonus(policy, strategy)), concerns


def _entry_for_segment(
    index: int,
    segment,
    output_start: int,
    *,
    transition: str,
    reason: str,
) -> TimelineEntry:
    return TimelineEntry(
        index=index,
        role=segment.role,
        source_start_ms=segment.source_start_ms,
        source_end_ms=segment.source_end_ms,
        output_start_ms=output_start,
        output_end_ms=output_start + max(0, segment.source_end_ms - segment.source_start_ms),
        sentence_start=segment.sentence_start,
        sentence_end=segment.sentence_end,
        transition=transition,  # type: ignore[arg-type]
        key_point=segment.key_point,
        reason=reason,
    )


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted((int(start), int(end)) for start, end in intervals if end > start):
        if merged and start <= merged[-1][1] + OVERLAP_TOLERANCE_MS:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _speech_overlap(start_ms: int, end_ms: int, speech: list[tuple[int, int]]) -> int:
    return sum(
        max(0, min(end_ms, speech_end) - max(start_ms, speech_start))
        for speech_start, speech_end in speech
    )


def build_timeline(
    plan: RetentionPlan,
    *,
    strategy: str,
    hook_start_ms: int | None = None,
    hook_end_ms: int | None = None,
    hook_text: str = "",
    hook_direction: str = "none",
    loop_start_ms: int | None = None,
    loop_end_ms: int | None = None,
    speech_intervals: list[tuple[int, int]] | None = None,
) -> TimelineReport:
    """Map disjoint source spans into output order and check the result.

    This is the single source of truth for preview and export: both use the same
    entries. It never fudges an overlap; duplicated source time is reported so a
    render with repeated speech cannot be described as a clean edit.

    ``speech_intervals`` makes the missing-coverage check honest. Pacing removes
    silence between sentences, which is not missing speech. When sentence spans
    are supplied, only a gap that overlaps actual speech invalidates the plan.
    """
    entries: list[TimelineEntry] = []
    warnings: list[str] = []
    cursor = 0
    order = _strategy_order(plan, strategy)

    hook_allowed = (
        hook_start_ms is not None
        and hook_end_ms is not None
        and (strategy == "cold_open" or hook_direction == "before")
    )
    if hook_allowed and hook_end_ms > hook_start_ms:
        entries.append(
            TimelineEntry(
                index=0,
                role="hook",
                source_start_ms=int(hook_start_ms),
                source_end_ms=int(hook_end_ms),
                output_start_ms=0,
                output_end_ms=int(hook_end_ms) - int(hook_start_ms),
                transition="continuous",
                key_point=hook_text[:220],
                reason=(
                    "A grounded later line opens the clip as a cold open."
                    if strategy == "cold_open"
                    else "A grounded earlier line opens the clip as context."
                ),
            )
        )
        cursor = entries[-1].output_end_ms

    first_main = True
    for position, segment_index in enumerate(order):
        segment = plan.segments[segment_index]
        transition = "cut"
        if strategy == "cold_open" and first_main and entries:
            transition = "flashback"
        elif strategy in {"result_first", "in_medias_res"} and position == 0:
            transition = "cut"
        elif strategy in {"result_first", "in_medias_res"} and position == 1:
            transition = "flashback"
        elif position == 0:
            transition = "continuous"
        reason = (
            "Opens the flashback to the setup."
            if transition == "flashback"
            else "Kept in output order by the selected strategy."
        )
        entry = _entry_for_segment(
            len(entries), segment, cursor, transition=transition, reason=reason
        )
        entries.append(entry)
        cursor = entry.output_end_ms
        first_main = False

    if (
        strategy == "loop"
        and loop_start_ms is not None
        and loop_end_ms is not None
        and loop_end_ms > loop_start_ms
    ):
        entries.append(
            TimelineEntry(
                index=len(entries),
                role="loop",
                source_start_ms=int(loop_start_ms),
                source_end_ms=int(loop_end_ms),
                output_start_ms=cursor,
                output_end_ms=cursor + int(loop_end_ms) - int(loop_start_ms),
                transition="cut",
                key_point="Repeats the opening beat.",
                reason="Replays the opening beat so the clip can loop without a hard cut.",
            )
        )
        cursor = entries[-1].output_end_ms

    overlaps: list[dict] = []
    duplicated = 0
    looped = 0
    for left_index, left in enumerate(entries):
        for right in entries[left_index + 1 :]:
            intersection = min(left.source_end_ms, right.source_end_ms) - max(
                left.source_start_ms, right.source_start_ms
            )
            if intersection > OVERLAP_TOLERANCE_MS:
                if "loop" in {left.role, right.role}:
                    # A loop replays the opening beat on purpose; it is not the
                    # accidental duplication this check exists to catch.
                    looped += intersection
                    continue
                duplicated += intersection
                overlaps.append(
                    {
                        "first": left.index,
                        "second": right.index,
                        "source_overlap_ms": intersection,
                        "reason": "The same source time would play twice.",
                    }
                )

    main_entries = [entry for entry in entries if entry.role not in {"hook", "loop"}]
    main_start = min((entry.source_start_ms for entry in main_entries), default=0)
    main_end = max((entry.source_end_ms for entry in main_entries), default=0)
    speech = _merge_intervals(speech_intervals) if speech_intervals is not None else None
    gaps: list[dict] = []
    if main_entries:
        intervals = sorted((entry.source_start_ms, entry.source_end_ms) for entry in main_entries)
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + OVERLAP_TOLERANCE_MS:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        covered = sum(end - start for start, end in merged)
        for left, right in zip(merged, merged[1:]):
            gap_start, gap_end = left[1], right[0]
            if gap_end <= gap_start:
                continue
            speech_ms = _speech_overlap(gap_start, gap_end, speech) if speech is not None else 0
            gaps.append(
                {
                    "source_start_ms": gap_start,
                    "source_end_ms": gap_end,
                    "duration_ms": gap_end - gap_start,
                    "speech_ms": speech_ms,
                    "reason": (
                        "This gap contains speech that the plan would skip."
                        if speech_ms > COVERAGE_TOLERANCE_MS
                        else "Silence removed between kept sentences."
                    ),
                }
            )
    else:
        covered = 0
    span = max(0, main_end - main_start)
    if speech is None:
        # Legacy callers without transcript spans keep the conservative check.
        missing = max(0, span - covered)
    else:
        missing = sum(gap["speech_ms"] for gap in gaps)

    if duplicated:
        warnings.append(
            f"{duplicated / 1000:.1f}s of source speech would be duplicated; the plan is invalid."
        )
    if missing > COVERAGE_TOLERANCE_MS:
        warnings.append(
            f"{missing / 1000:.1f}s of source speech would be missing from the timeline."
        )
    for entry in main_entries:
        if entry.source_end_ms - entry.source_start_ms < 500:
            warnings.append("A timeline entry is shorter than half a second; captions may flicker.")
            break
    valid = duplicated == 0 and missing <= COVERAGE_TOLERANCE_MS
    return TimelineReport(
        entries=entries,
        total_output_ms=cursor,
        covered_ms=covered,
        missing_ms=missing,
        duplicated_ms=duplicated,
        loop_ms=looped,
        gaps=gaps,
        overlaps=overlaps,
        valid=valid,
        warnings=warnings,
    )


def _empty_alternative(plan: RetentionPlan, strategy: str) -> EditorialAlternative:
    profile = STRATEGIES[strategy]
    return EditorialAlternative(
        id=strategy,
        label=profile.label,
        strategy=profile.id,
        description=profile.description,
        available=False,
        confidence=0.0,
        estimated_duration_ms=_main_duration(plan),
        pacing_profile=plan.profile,
        reason="Not enough structure to evaluate this strategy.",
    )


def plan_editorial(
    plan: RetentionPlan,
    model: TranscriptModel,
    *,
    policy: EditorialPolicy | dict | None = None,
    duration_preset: str = "extended",
) -> RetentionPlan:
    """Choose, explain and attach the editorial decision to a retention plan."""
    if policy is None:
        policy = EditorialPolicy()
    if isinstance(policy, dict):
        policy = EditorialPolicy.model_validate(policy)

    if len(plan.segments) < 2:
        decision = EditorialDecision(
            strategy="chronological",
            label=STRATEGIES["chronological"].label,
            style=policy.style,
            rationale="The moment is a single beat; reordering cannot improve it.",
            confidence=plan.confidence,
            alternatives=[_empty_alternative(plan, "chronological")],
            pacing_profile=plan.profile,
        )
        plan.strategy = "chronological"
        plan.rationale = decision.rationale
        plan.alternatives = decision.alternatives
        plan.editorial = decision
        return plan

    alternatives = _alternatives(plan, model, policy)
    cold_ok, cold_reason = _cold_open_available(plan, policy)
    loop_ok, loop_reason = _loop_available(plan, policy)
    available = {
        alternative.strategy: alternative.available
        for alternative in alternatives
        if ":" not in alternative.id
    }
    concerns_by_strategy: dict[str, list[str]] = {}
    scores: dict[str, float] = {}
    for strategy in ("chronological", "cold_open", "result_first", "in_medias_res",
                     "question_answer", "story_escalation", "reaction_led"):
        if not available.get(strategy):
            continue
        score, concerns = _score_strategy(
            strategy, plan, policy, cold_reason=cold_reason, cold_ok=cold_ok
        )
        scores[strategy] = score
        concerns_by_strategy[strategy] = concerns

    requested = policy.preferred_strategy
    fallback = ""
    review_required = plan.review_required
    concerns: list[str] = []

    if policy.style == "chronological":
        selected = "chronological"
        rationale = "You asked for chronological order, so no source span was moved."
        confidence = scores.get("chronological", plan.confidence)
    elif policy.style == "manual":
        selected = "manual"
        rationale = "You are ordering this cut manually; Clipforge did not reorder it."
        confidence = 1.0
    elif requested == "chronological":
        selected = "chronological"
        rationale = "You chose the chronological strategy; it is always honoured."
        confidence = scores.get("chronological", plan.confidence)
    elif requested not in {"automatic", "manual"}:
        if available.get(requested):
            selected = requested
            rationale = f"You chose {STRATEGIES[requested].label}; the source supports it."
            confidence = scores.get(requested, plan.confidence)
            concerns = concerns_by_strategy.get(requested, [])
        else:
            selected = "chronological"
            rationale = (
                f"You chose {STRATEGIES.get(requested, STRATEGIES['chronological']).label}, but "
                "this source cannot support it. The chronological baseline is shown instead."
            )
            fallback = requested
            review_required = True
            confidence = scores.get("chronological", plan.confidence)
    else:
        # Automatic: a qualified cold open is the preferred default, otherwise the
        # highest-scoring available strategy wins. Chronological stays in the list.
        candidates = [strategy for strategy in scores if strategy != "chronological"]
        selected = "chronological"
        if policy.style == "hook_first" and cold_ok:
            selected = "cold_open"
        elif candidates:
            best = max(candidates, key=lambda strategy: scores[strategy])
            if scores.get(best, 0) > scores.get("chronological", 0):
                selected = best
            elif cold_ok and scores.get("cold_open", 0) + 0.03 >= scores.get("chronological", 0):
                selected = "cold_open"
        if selected == "chronological":
            rationale = (
                "Source order already reads clearly and no reorder beat the chronological baseline."
            )
        elif selected == "cold_open":
            hook = _hook_external_after(plan)
            rationale = (
                "A grounded later line makes a stronger opening, and the moment resolves it; "
                "the clip opens on that line and flashes back to the setup."
            )
            if hook is not None:
                rationale += f" The line is {hook.quality:.0f}/100 and shares wording with the moment."
        elif selected == "result_first":
            rationale = "The payoff lands first, then the clip returns to how it happened."
        elif selected == "in_medias_res":
            rationale = "The clip starts inside the action, then returns to the context that explains it."
        elif selected == "question_answer":
            rationale = "The moment already opens with a question and resolves it in source order."
        elif selected == "story_escalation":
            rationale = "The beats already escalate through development to the payoff."
        elif selected == "reaction_led":
            rationale = "The moment already opens on a reaction, so the reaction leads."
        else:
            rationale = "The selected strategy best matches the moment's structure."
        confidence = scores.get(selected, plan.confidence)
        concerns = concerns_by_strategy.get(selected, [])

    if selected == "manual":
        confidence = 1.0

    if selected in {"result_first", "in_medias_res"}:
        review_required = True
        concerns.append("This reordered edit needs a person to watch the joins before publishing.")
    if selected == "loop":
        review_required = True
        concerns.append(
            "The clip deliberately replays its opening beat to close the loop; watch the seam before publishing."
        )
        if not loop_ok:
            selected = "chronological"
            fallback = fallback or "loop"
            concerns.append(loop_reason)
    if fallback:
        concerns.append(
            f"{STRATEGIES[fallback].label} was requested but unavailable; "
            "a chronological cut was used instead."
        )
    if selected == "cold_open" and plan.context_loss > 0.3:
        review_required = True
    if not available.get(selected, True) and selected != "manual":
        # Defensive: never persist a selected strategy the planner marked unavailable.
        selected = "chronological"
        fallback = fallback or "selected"
        review_required = True

    order = _strategy_order(plan, selected)
    reordered = order != list(range(len(plan.segments))) or selected in {"cold_open", "loop"}
    hook = _hook_document(plan)
    # A cold open moves a later line to the front. An earlier line can also open
    # the clip as context, which keeps source order and needs no approval.
    hook_in_timeline = hook is not None and (
        selected == "cold_open" or hook.direction == "before"
    )
    loop = _loop_span(plan) if selected == "loop" else None
    timeline = build_timeline(
        plan,
        strategy=selected,
        hook_start_ms=hook.start_ms if hook_in_timeline else None,
        hook_end_ms=hook.end_ms if hook_in_timeline else None,
        hook_text=hook.text if hook is not None else "",
        hook_direction=hook.direction if hook is not None else "none",
        loop_start_ms=loop[0] if loop else None,
        loop_end_ms=loop[1] if loop else None,
        speech_intervals=[(sentence.start_ms, sentence.end_ms) for sentence in model.sentences],
    )
    if not timeline.valid:
        review_required = True
        concerns.extend(timeline.warnings)
        # A timeline with duplicated or missing speech must not be rendered as if
        # it were a clean edit. Fall back to source order and say so.
        selected = "chronological"
        fallback = fallback or "timeline_invalid"
        order = _plan_order(plan)
        reordered = False
        timeline = build_timeline(
            plan,
            strategy="chronological",
            speech_intervals=[(sentence.start_ms, sentence.end_ms) for sentence in model.sentences],
        )
        concerns.append("Timeline checks failed, so the chronological baseline was used.")

    profile = STRATEGIES.get(selected, STRATEGIES["chronological"])
    decision = EditorialDecision(
        strategy=profile.id if selected in STRATEGIES else "chronological",
        label=profile.label if selected in STRATEGIES else STRATEGIES["chronological"].label,
        style=policy.style,
        rationale=rationale,
        confidence=round(_clamp(confidence), 3),
        alternatives=alternatives,
        concerns=list(dict.fromkeys(concerns)),
        review_required=review_required,
        reordered=reordered,
        applied=reordered,
        fallback=fallback,
        pacing_profile=plan.profile,
        timeline=timeline,
    )
    plan.strategy = decision.strategy
    plan.rationale = decision.rationale
    plan.alternatives = decision.alternatives
    plan.timeline = timeline
    plan.reordered = decision.reordered
    plan.editorial = decision
    plan.review_required = review_required
    plan.warnings = list(dict.fromkeys([*plan.warnings, *decision.concerns]))
    return plan


def duration_note(preset: str) -> str:
    """Short, honest label used in candidate metadata and the editorial preview."""
    if preset == "extended":
        return "Extended is a 60–90s editorial target, not a platform limit."
    return ""
