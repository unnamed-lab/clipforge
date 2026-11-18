# Context-aware AI packaging

Date: 2026-11-18

Scope: the packaging layer that turns an existing editorial clip into titles,
descriptions, thumbnail text, keywords and hashtags. Trend research is unchanged
and is documented separately in `AI_PACKAGING_TREND_CHECK.md`; this document
supersedes the packaging sections of that file.

The comparison outputs in section 6 come from the deterministic path on a
synthetic transcript fixture. They are not customer results and no performance
metric is claimed for any of them.

## 1. Summary

Before this change, packaging could only see the clip. The deterministic
generator derived titles, keywords and descriptions from `clip_text()` (the
sentences inside the clip window), and the optional LLM prompt contained
`clip_transcript[:6000]` plus the first 20 clip sentences. If the clip opened
with "Welcome back to the show" and the actual insight arrived forty seconds
later, packaging had no way to know the insight existed. That is the root cause
of the shallow, opening-line-led packaging, and no amount of prompt tuning could
fix it because the information was never in the request.

The fix is a separate analysis layer plus a directing stage:

- a **content brief** built from the whole transcript, cached and reusable, that
  records the clip's real subject, central claim, strongest hook, supporting
  points, examples, caveats, wider-video context and evidence;
- a versioned **packaging director** (`clipforge-packaging-director`) that
  generates candidates across eight title angles, scores them on an editorial
  rubric, rejects unsupported claims before ranking, and writes descriptions,
  thumbnail text and a stated content angle from the brief;
- a **free-first research layer** that is off by default, never invents a
  metric, and reports honestly when a provider is unavailable or needs manual
  research;
- **field-level regeneration and edit preservation**, so approved metadata is
  not silently overwritten.

Packaging still works with no research provider, no API key, no GPU and no paid
service. The LLM path remains optional and is gated by the same accuracy checks
as the deterministic path.

## 2. What the audit found

### 2.1 Evidence

| Layer | Before | Effect |
| --- | --- | --- |
| Deterministic generator (`packaging/generator.py` at `HEAD`) | `generate_metadata(context)` read `clip_text(transcript, candidate)` only | Titles, keywords and descriptions were restatements of the clip's own sentences |
| LLM prompt (`packaging/llm.py` at `HEAD`) | `"clip_transcript": text.full_text[:6000]` and the first 20 clip sentences | The model never saw earlier setup or later payoff outside the clip |
| Title generation | `_title_variants()` added verbatim sentences, a hook and an opening clause | No angle, no scoring, no rejection reason; the first sentence usually won |
| Description | Concatenated clip sentences; long form appended hashtags | Read like a transcript dump, not an explanation |
| Keywords | Accepted sentence-length strings | Unusable as search phrases |
| Topic fallback | Joined the three highest-ranked terms | Produced non-phrases such as "always cost customers" |
| Regeneration | Whole-document replacement | Manual edits were at risk |
| Research | Referenced Trends/vidIQ as if generally available | Neither is usable without approval or a verified connection |

### 2.2 Root cause

The packaging request had no representation of the video outside the clip
window. Everything downstream — prompt, candidates, rubric — inherited that
blindness. The missing piece was an analysis artefact, not a better prompt.

### 2.3 Secondary defects confirmed during the audit

- No provenance: the document did not record which brief, prompt version or
  research state produced the text.
- No distinction between "the clip's topic" and "the video's topic", so a
  multi-topic interview contaminated every field.
- No way to explain why a title was chosen, or why an alternative was rejected.
- Editing a caption or composition had no defined relationship to packaging;
  regeneration had no field-level contract.

## 3. What changed

### 3.1 New and changed modules

| File | Responsibility |
| --- | --- |
| `packaging/brief.py` | Whole-transcript sectionising (topic shift, silence gaps, speaker change), section summaries, evidence retrieval around the clip, clip classification, claim/hook/example/caveat/takeaway detection, entities, search concepts, `brief_key` cache identity, `validate_brief_claims` |
| `packaging/director.py` | Accuracy gate, editorial scoring, title candidates across eight angles, description variants, thumbnail text, content angle |
| `packaging/skill.py` | Versioned skill manifest, system prompt, user prompt, configurable rubric weights |
| `packaging/models.py` | `ContentBrief`, `BriefEvidence`, `SectionSummary`, `TitleScores`, extended `TitleVariant`, research result models, v2 `ClipMetadata` and `PackagingDocument` |
| `packaging/generator.py` | `generate_metadata(context, brief, research)`; brief-aware titles, descriptions, thumbnail text, keywords, hashtags, audience, takeaway and warnings |
| `packaging/llm.py` | Brief-driven prompts; LLM titles re-scored through the same gate; descriptions parsed as an object; provenance and omission reporting |
| `packaging/research/{base,providers,service}.py` | Manual, opt-in YouTube autocomplete, approved Google Trends bridge, vidIQ status, YouTube public-video metadata; TTL cache; honest state combination |
| `api/routes_packaging.py` | Source metadata, brief reuse by `brief_key`, research orchestration, field-level regeneration, edit preservation, config endpoint |
| `api/models.py` | `fields`, `force_fields`, `edited_fields`, `title_angle`, `more_titles`, `research_mode`, `manual_terms`, `reuse_brief`, `metadata` |
| `config.py` | Research settings and rubric override |
| `packages/shared/src/schemas.ts` | Shared v2 document, brief, research and metadata schemas |
| `apps/web/.../packaging-panel.tsx` | Research controls, title angle, more titles, manual terms, per-field regeneration, edited-field tracking, brief and research cards |

No parallel packaging stack was introduced: the existing provider registry
(`providers/llm/openai_compat.py`), transcript store, project model, Client
Profiles, API client and panel were extended in place.

### 3.2 Data flow

```text
transcript + editorial timeline
        │
        ├── content brief ──► cached by brief_key (source + clip timeline)
        │        │
        │        ├── clip classification, claim, hook, evidence, caveats
        │        └── hierarchical summaries + omissions for long videos
        │
        ├── research (off | free | enhanced) ──► observations with provenance
        │
        └── packaging director
                 ├── 8 title angles → accuracy gate → weighted ranking
                 ├── descriptions (platform limits, clip voice)
                 ├── thumbnail text (character budget, no dangling words)
                 └── keywords / hashtags (grounded phrases only)
                        │
                        └── deterministic metadata (or validated LLM metadata)
                                │
                                └── field preservation → document v2 → UI
```

### 3.3 Accuracy gate

Titles are rejected, not silently dropped, when they:

- contain a number the clip does not contain;
- contain a proper noun the clip or supplied context does not establish;
- use a banned promotional claim or generic clickbait phrase;
- exceed the platform title limit;
- share no concept with the selected clip (deterministic path only; LLM
  paraphrases are exempt from this one heuristic, not from the others);
- are built mainly from wider-video context rather than the clip;
- open with a bare reference such as "this", "that" or "it", which cannot stand
  alone as a title.

Rejected candidates stay in the response with `accepted = false` and a reason.
Ranking applies the gate before weighting, and the rubric is documented as
editorial fit, never as a click-through prediction.

### 3.4 Scoring rubric

Default weights, overridable with `CLIPFORGE_PACKAGING_TITLE_WEIGHTS_JSON`:

| Signal | Weight |
| --- | --- |
| Accuracy | 0.30 |
| Specificity | 0.20 |
| Audience relevance | 0.20 |
| Curiosity | 0.15 |
| Search relevance | 0.10 |
| Readability | 0.05 |

`ACCURACY_GATE = 0.65` is applied before the weighted score is used.

## 4. Workflow

1. The user selects a clip. The API loads the stored document, project,
   Client Profile and effective packaging settings.
2. Source title and description are read from the library row, falling back to
   the project goals.
3. A `brief_key` is computed from the transcript, the clip timeline and the
   source title. If the stored brief matches and the user did not ask for a
   rebuild, the brief is reused and a warning says so.
4. Otherwise the brief is built from the whole transcript: sentences are
   flattened, the transcript is sectioned by topic shift, silence and speaker,
   sections are summarised, and evidence around the clip is retrieved. Long
   videos are summarised hierarchically; omitted sections are counted and
   reported.
5. Research runs only if requested. `off` makes no external call; `free` uses
   the configured free adapters; `enhanced` adds optional enrichment adapters.
6. The director generates candidates, applies the gate, ranks accepted titles,
   and writes descriptions, thumbnail text and the content angle.
7. The deterministic result can be replaced by a validated LLM result. If the
   LLM is unavailable or its draft fails validation, deterministic metadata is
   returned with a warning.
8. Field selection decides what is regenerated. Without an explicit selection,
   previously edited fields are preserved; `force_fields` regenerates them
   deliberately.
9. The document stores metadata, brief, research, skill version, brief metadata
   and the edited-field list, then returns the candidate and document.
10. Caption or composition edits never call this endpoint, so they cannot
    trigger packaging work.

## 5. Providers, configuration and verified limits

### 5.1 Research modes

| Mode | Behaviour |
| --- | --- |
| `off` (default) | No external call. Packaging is source-only. |
| `free` | Manual terms, opt-in YouTube suggestions when enabled, YouTube public metadata when an API key exists, approved Trends bridge when configured |
| `enhanced` | Free providers plus optional enrichment such as vidIQ, reported as unavailable unless a verified connection is asserted |

Manual terms always work and require no key. They are treated as terminology,
not evidence of demand.

### 5.2 Provider status

| Provider | What it can return | Access | Verified limit / status | Clipforge behaviour |
| --- | --- | --- | --- | --- |
| Manual terms | User-supplied phrasing | None | Always available | Used as terminology with `manual` provenance |
| YouTube autocomplete | Suggestion strings | None, but opt-in | Undocumented public endpoint; no published quota; can be blocked or changed | Off by default; failures degrade to unavailable; never treated as volume |
| YouTube Data API v3 (`search.list`) | Public video titles/descriptions | API key | 100 quota units per `search.list`; default 10,000 units/day ≈ 100 searches/day | Reuses the existing trend provider key; cached; no search volume |
| Google Trends | Relative interest (0–100) | Approved endpoint | Official API is application-gated; consumer-site scraping is not implemented | Queries only an operator-configured bridge; otherwise `manual_required` |
| vidIQ | Manual research in the vidIQ product | Subscription + AI client | No verified backend REST API for this use; integration path is an MCP/OAuth AI client | `operational = false`; never claims a connection; manual fallback |
| Local LLM / OpenAI-compatible endpoint | Phrasing | Operator configured | Optional; validation-gated | Never trusted with facts; rejected drafts fall back to deterministic output |

### 5.3 Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIPFORGE_YOUTUBE_SUGGEST_ENABLED` | `false` | Opt in to the undocumented suggestions endpoint |
| `CLIPFORGE_TRENDS_ENDPOINT` | empty | Approved Trends-compatible bridge URL |
| `CLIPFORGE_TRENDS_API_KEY` | empty | Credential for that bridge |
| `CLIPFORGE_VIDIQ_ENABLED` | `false` | Records that an operator asserts a vidIQ connection; enables no backend call |
| `CLIPFORGE_PACKAGING_TITLE_WEIGHTS_JSON` | empty | Overrides rubric weights, clamped and normalised |
| `CLIPFORGE_PACKAGING_PROFILES_JSON` | empty | Existing per-platform limit overrides |

All of these are documented with placeholders in `.env.example`.

### 5.4 What is never generated

- Search volume, difficulty, ranking or trend velocity.
- Competitor performance or audience size.
- Statistics, quotations, names, links or CTA copy that the source does not
  contain.
- Virality, reach or click-through predictions, in copy or in the rubric.

When a provider returns nothing, the field stays absent and the document records
why.

## 6. Old versus new output

Fixture: an eleven-sentence synthetic interview excerpt covering fundraising,
hiring and pricing. The clip covers sentences 5–9 (the hiring segment). Source
title: "Founder lessons: hiring, fundraising and pricing". Platform: YouTube
Shorts. The "old" column is the generator at `HEAD`, run against the same input;
the "new" column is the current deterministic path.

### 6.1 Titles

| Old | New |
| --- | --- |
| 5 candidates, all verbatim clip sentences in spoken order; one style (`direct`) and a provenance note, but no angle variety, scoring or rejection reason | 6 candidates across descriptive, search, insight and story angles; 4 accepted with weighted scores, 2 rejected with reasons |

New top four:

| Title | Angle | Weighted | Reason |
| --- | --- | --- | --- |
| The biggest mistake founders make is hiring for the role they think | descriptive | 0.699 | Uses another explicit statement from the clip, not the opening line |
| Hiring: you should hire for the problem you actually have | search | 0.670 | Pairs the clip's topic phrase with its main point |
| You should hire for the problem you actually have right now | descriptive | 0.606 | States the clip's central claim directly |
| We spent six months looking for a head of sales before we | story | 0.595 | Leads with a concrete example from the clip |

Rejected, and shown as rejected:

| Title | Reason |
| --- | --- |
| This only works if you actually listen instead of pitching | Opens with a reference that has no antecedent in a title |
| That was a very expensive way to learn a simple lesson | Same |

### 6.2 Descriptions

Old short description (150-character limit):

> The biggest mistake founders make is hiring for the role they think they need.
> You should hire for the problem you actually have right now.

Old long description appended hashtags and reused the same sentences as the
caption.

New:

> **Short (200-character limit):** This clip looks at hiring. It makes the case
> that you should hire for the problem you actually have right now.

> **Long (5,000-character limit):** This clip looks at hiring. The clip's main
> point is that you should hire for the problem you actually have right now. It
> uses a concrete example: we spent six months looking for a head of sales
> before we had a single customer. The speaker adds one qualification: this only
> works if you actually listen instead of pitching.

The short description leads with a claim that can stand alone; a caveat that
begins with "this" is used later, where its antecedent exists.

### 6.3 Keywords

| Old | New |
| --- | --- |
| `startup hiring education`; then three of the four entries were complete spoken sentences | `startup hiring`; `mistake founders` |

Old keyword entries included full spoken sentences of up to 80 characters. New
entries are 2–5 word phrases with no sentence punctuation, no dangling function
word and no duplicate stem; concepts that would bridge a dropped word are
discarded rather than trimmed into nonsense.

### 6.4 Thumbnail text

| Old | New |
| --- | --- |
| "The biggest mistake founders make is" (verbatim sentence prefix, cut mid-thought) | "Hire for the problem you actually have" (38 of 40 characters, ends on a content word) |

### 6.5 Hashtags

Both versions use the same platform-derived set in this fixture
(`#startuphiring #foundermistakes #education #shorts`). The difference is that
hashtags can now carry research provenance, are capped per platform, and cannot
introduce a term that the transcript, profile or a recorded provider response
did not supply.

### 6.6 Document metadata

The new document records `PACKAGING_VERSION = 2`, the brief version and whether
it was cached, the number of omitted sections, the skill version, the research
mode and provider statuses, the edited-field list, and grounding notes such as
"The packaging director used a content-brief/1.0 brief covering N source
sections (M omitted from the prompt)."

## 7. Evaluation and verification

### 7.1 Packaging intelligence suite

`apps/worker/tests/test_packaging_intelligence.py` adds 31 tests (24 test
functions, one of which is an eight-case evaluation set). It covers:

- a later insight being selected instead of the opening line;
- clip subject kept distinct from the wider video;
- hierarchical summarising and omission reporting for long transcripts;
- ambiguous clips being labelled rather than guessed;
- title diversity, grounding, scoring and rejection reasons;
- the accuracy gate rejecting invented numbers, names and context leakage;
- descriptions explaining the clip instead of copying it;
- platform-specific limits and description style;
- short descriptions leading with a standalone claim;
- thumbnail text standing alone within the platform limit;
- keywords being phrases, not sentences or fragments;
- clip topics never joining unrelated terms;
- metadata not reusing the same text in every field;
- research off making no provider call;
- provider failure not blocking packaging;
- manual terms working without any key;
- unavailable providers reporting manual research without metrics;
- cache preventing repeated provider calls;
- research terms being source-grounded and able to enrich keywords;
- the skill being versioned and documenting its safety rules;
- a manual title edit surviving full regeneration;
- field regeneration keeping other approved fields;
- the config endpoint reporting the skill and provider states.

The eight-case evaluation set asserts, for each case, that titles, short and
long descriptions exist, validation passes, at least one title is accepted, the
top title is not simply the first clip sentence, and no two titles are
duplicates. Cases: slow opening, multi-topic, tutorial, story, technical,
caveated, ambiguous, misleading candidate.

### 7.2 Commands run

| Check | Result |
| --- | --- |
| `apps/worker/.venv/Scripts/python.exe -m pytest -q` | 234 passed |
| `apps/worker/.venv/Scripts/python.exe -m ruff check src tests` | All checks passed |
| `pnpm --filter @clipforge/web typecheck` | No type errors |
| `pnpm --filter @clipforge/web lint` | No lint errors |
| `pnpm --filter @clipforge/web test` | 8 passed |
| `pnpm --filter @clipforge/web build` | Next.js production build succeeded |

### 7.3 Not verified in a browser

The packaging panel was not driven in a real browser during this work because
the session had no Playwright MCP tools available. Type checking, linting, unit
tests and the production build are the evidence for the UI changes; the
end-to-end flow (select a clip, generate, edit a field, regenerate one field,
switch platform, switch research mode) still needs a browser pass on a project
with an analysed transcript.

## 8. Costs, blockers and limits

### 8.1 Costs

- Free path: no paid service, no GPU, no account.
- YouTube Data API: free within quota; beyond roughly 100 searches per day a
  quota increase or a different source is required.
- Google Trends: no cost through an approved bridge, but access requires an
  application/approval; no scraping fallback exists.
- vidIQ: subscription product; not integrated as a backend provider.
- LLM: optional and operator-funded; every request already tolerates its
  absence.

### 8.2 Blockers

| Blocker | Impact |
| --- | --- |
| No Playwright tools in this session | UI changes are type-checked and built, not browser-verified |
| Google Trends official API is application-gated | `manual_required` until an approved endpoint is configured |
| vidIQ has no verified backend REST API | Manual research only; no automatic enrichment |
| YouTube quota | About 100 `search.list` calls per day by default |

### 8.3 Known limitations

- Brief extraction is heuristic (sentence features, section boundaries, lexical
  retrieval), not semantic understanding. It is deterministic and auditable, and
  it reports ambiguity, but it can mislabel an unusual structure.
- Keyword research is lexical. It supplies terminology, not demand; no volume is
  inferred anywhere.
- Keyword extraction may keep a locally adjacent pair that reads as a fragment
  ("mistake cost"). Phrase formation is conservative, and titles carry the
  stronger editorial weight.
- The caption still quotes the clip's opening sentences by design; only titles,
  descriptions, thumbnail text, keywords and hashtags were moved to the brief.
- The rubric is editorial. Scores are not calibrated against click-through or
  retention, and should not be presented as predictions.

### 8.4 Next steps

1. Browser-verify the panel end to end and add a Playwright regression for
   field regeneration and edit preservation.
2. Add a brief-quality fixture set from real customer transcripts and review
   section boundaries and claim selection with an editor.
3. Decide whether to apply for official Trends access or keep the approved
   bridge as the only path.
4. If vidIQ remains manual, document the manual workflow in-product rather than
   leaving the disabled provider visible without explanation.
5. Consider a caption variant built from the brief, keeping the current verbatim
   caption as one of the available styles.
