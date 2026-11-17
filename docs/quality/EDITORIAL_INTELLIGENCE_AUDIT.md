# Editorial intelligence audit

Date: 2026-11-17

Scope: the duration preset, grounded hook, narrative planning and explainable
strategy work added on top of the existing Clipforge analysis and FFmpeg render
pipeline. This audit records what is implemented, what is deliberately not
implemented, how it is validated, and what remains a limitation.

## 1. Baseline and non-goals

The existing pipeline remains the only media pipeline:

1. probe and import the source;
2. transcribe with the configured `faster-whisper` provider;
3. build sentence/word models and candidate drafts;
4. score candidates and attach hooks;
5. plan retention, strategy and a source-to-timeline map;
6. render with the existing FFmpeg service;
7. validate the output with the quality gate.

No second renderer, fake timeline, hard-coded timestamp patch or separate
"editorial" export path was added. Editorial planning is renderer-independent:
analysis stores a plan, and the renderer consumes the same plan that the preview
shows.

## 2. Duration presets

Implemented in `apps/worker/src/clipforge/durations.py`:

| Preset | Target |
| --- | --- |
| Short | 15–30 s |
| Standard | 30–60 s |
| Extended (default) | 60–90 s |
| Custom | validated minimum/maximum |

Rules enforced:

- A preset is an editorial target, not a platform upload limit.
- Custom ranges are validated against `MIN_CUSTOM_DURATION_MS` (2 s) and
  `MAX_CUSTOM_DURATION_MS` (15 min).
- Duration is a ranking preference, not a hard filter. Scores use 88 % content
  and 12 % duration proximity.
- Candidates outside the target are labelled `near`, `too_short`, `too_long` or
  `unknown`. They are never padded, stretched or truncated to fit.
- No-fit analysis reports the closest coherent alternatives.
- The render gate measures the actual output duration, not the requested target.
- `actual_ms` is recalculated from the planned timeline, because pause removal
  and a cold-open hook change the output duration.
- Changing only the duration merges onto the saved project/profile brief rather
  than replacing the rest of the brief.

Legacy behaviour: old briefs without a preset infer one from their saved
`min/max_duration_ms`. Only documents with no duration data at all fall back to
Extended.

## 3. Duration structure

`apps/worker/src/clipforge/editorial/narrative.py` allocates narrative roles from
the selected duration structure:

- Short: one idea, hook and payoff.
- Standard: hook/context, main and payoff.
- Extended: hook/context, main, development and payoff.

Roles are metadata over contiguous source spans. They do not invent dialogue and
do not move content unless the selected strategy explicitly reorders spans.

## 4. Hooks

`apps/worker/src/clipforge/hooks/select.py` now produces grounded candidates with:

- strategy (`question`, `outcome`, `curiosity`, `story`, `humour`, `emotional`,
  `contrarian`, `chronological`, `manual`);
- direction (`opening`, `before`, `after`, `none`);
- source (`candidate`, `external`, `none`);
- grounded flag, quality score, component scores, reason and warnings;
- `needs_context` and `review_required`.

`direction="after"` is the only case that can become a cold open. A hook that is
not grounded in the source transcript is not approved automatically. If a brief
requires a hook and none fits, the plan records a warning and requires review.

## 5. Editorial strategies

Implemented and genuinely applied:

- Chronological (first-class baseline; always available).
- Cold open → flashback → payoff, using a qualified later grounded line.
- Result/reveal first.
- In medias res.
- Question → answer → evidence → conclusion.
- Escalation → climax.
- Reaction-led.

Semantically selected but not reordering (labelled honestly):

- Montage/jump cuts: needs shot-level coverage the source does not provide.
- Loop/callback: needs a rendered seam and matching frames.
- Open-loop explanation: needs a separate unanswered setup elsewhere.
- B-roll, reaction-shot invention, fake multi-camera and J/L cuts: unavailable
  because the source cannot support them without inventing coverage.

Strategy selection records:

- selected strategy and label;
- confidence;
- rationale;
- alternatives, including chronological, tighter and more conversational
  variants;
- concerns and review requirement;
- whether the plan reordered or was applied;
- fallback reason when a requested strategy is unavailable.

Automatic selection prefers a qualified cold open, then the highest-scoring
available strategy above the chronological baseline. Chronological remains an
explicit option and the safe fallback.

## 6. Timeline and edit validity

The timeline is the single source of truth for preview and export:

- each entry has source start/end, output start/end, role, transition, sentence
  range, key point and reason;
- captions are recalculated from the same source-to-output mapping;
- audio and video are cut from the same segment list;
- manual trim overrides generated editorial ordering;
- a reviewed non-linear plan is not applied unless `approve_editorial` is set;
- duplicate source speech is rejected;
- missing **speech** is rejected, while intentionally removed silence is listed
  as a gap and does not invalidate the edit;
- a timeline that fails validation forces chronological fallback and records
  `timeline_invalid`.

The check uses sentence spans, not just arithmetic on the candidate window. This
matters because balanced pacing removes pauses: a 0.5 s gap between two
sentences is silence, not missing speech.

## 7. Preview and review

`EditorialPlan` in the clip editor shows:

- actual cut duration, target range and fit status;
- strategy, confidence, review and applied badges;
- rationale and concerns;
- source → timeline table;
- alternatives with re-evaluation actions;
- an explicit approval checkbox for a reordered plan.

Changing editorial settings affects the next analysis. Re-evaluating changes the
candidate plan. Rendering applies a reordered plan only when it is approved (or
when the plan did not require approval).

## 8. Persistence and profile precedence

- `editorial_json` and `duration_json` are additive candidate columns applied
  through `_ADDED_COLUMNS`.
- Plans are stored verbatim, so the preview cannot claim an edit the renderer
  will not perform.
- Client Profile briefs, project overrides and request briefs are deep-merged:
  request > project > profile > legacy.
- Snapshotting and render profile snapshots are unchanged.
- A duration or editorial change does not re-transcribe, re-render or discard
  manual trims, captions, branding or composition overrides.

## 9. Observed real run

Source: YouTube `rbuml8f__wE`, 18:39, 1080p, imported through the existing URL
importer.

Observed before the timeline fix:

- candidate duration reasons were correct, but the API omitted `actual_ms`;
- several candidates fell back to chronological with `timeline_invalid` because
  pause gaps were counted as missing speech;
- one candidate was 89.4 s before pause removal.

The fix:

- adds `actual_ms` and `proximity` to the duration record;
- recalculates the duration record from the planned timeline;
- counts only speech overlap in missing-coverage checks;
- carries the updated duration through editorial re-evaluation and the UI.

## 10. Tests

`apps/worker/tests/test_editorial.py` covers:

- preset defaults, migration and custom-range validation;
- fit reporting without changing a cut;
- cold-open selection, timeline order and total output;
- chronological baseline and no reorder;
- unavailable-strategy fallback;
- duplicate-source rejection;
- silence removal versus missing speech;
- actual duration from the planned timeline;
- manual-trim precedence;
- approval before rendering a reviewed edit;
- plan preservation across duration and pacing changes.

Full worker suite: 147 tests passing after the change.

## 11. Remaining limitations

- Manual drag-and-drop ordering is represented as a style/strategy option but has
  no reorder editor yet.
- The editorial re-evaluation endpoint sees only the stored best hook, not the
  full hook candidate list.
- Montage, loop, open-loop, B-roll, reaction-shot synthesis, multi-camera and
  J/L cuts are reported unavailable rather than simulated.
- Scene detection remains the existing FFmpeg/cheap-feature classifier.
  PySceneDetect is an optional candidate, not a replacement.
- No claim is made that editorial scores predict retention or virality. They are
  deterministic transcript-based estimates.
- Browser UI verification is HTTP/API and build-based in this environment; no
  Playwright MCP was available.
