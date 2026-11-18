# AI packaging and trend check audit

Date: 2026-11-18

Scope: the AI Clip Packaging and optional Trend Check work added on top of the
existing analysis, editorial and FFmpeg render pipeline. This document records
what is implemented, which sources were verified, what needs credentials or
approval, what is mocked in tests, and what remains unavailable.

## 1. Non-goals and boundaries

- Packaging is metadata only. Generating or editing it never re-transcribes,
  re-renders, reorders or rewrites the clip.
- The existing editorial timeline remains the only source of clip truth. The
  packaging generator reads the same final timeline the preview and renderer
  use.
- No search volume, post count, growth percentage, ranking or engagement rate is
  estimated, interpolated or invented. A missing provider metric stays `None`.
- No restricted scraping or undocumented endpoint is used. TikTok and Instagram
  without approved official access are reported as manual research.
- No trend is applied automatically to a video. Selecting a trend may add one
  evidence-labelled hashtag to the caption; it does not cut, re-render or change
  the factual description.
- Publishing remains a separate explicit user action; this work does not
  publish anything.

## 2. Generated packaging

Implemented in `apps/worker/src/clipforge/packaging/`:

| Field | Behaviour |
| --- | --- |
| Title alternatives | Ranked grounded variants with style, reason, character count, platform limit and within-limit flag. |
| Short description | One or two spoken sentences, trimmed to the profile limit. |
| Long description | More clip context, optional CTA and hashtags within the profile limit. |
| Platform caption | Hook/opening plus supporting sentence, CTA, hashtags, whole-tag trimming and character count. |
| Hashtags | Small, deduplicated, categorized (`topic`, `niche`, `broad`, `trending`) with reason, provenance and relevance. |
| Search keywords | Natural-language phrases distinct from hashtags, `source="ai_generated"` and `verified=false`. |
| CTA | Only from profile text, custom text or a conservative goal default; never a new promise. |
| Thumbnail text | A short verbatim phrase from the clip, not a claim. |
| Validation | Separate issues and warnings; numbers and restricted claims are checked. |
| Provenance | Method, provider/model, transcript id, source start/end, sentence ids, grounding notes, warnings and timestamp. |

The deterministic path is always available offline. The optional LLM path
extends `OpenAiCompatibleScorer.complete_json`; if the endpoint is missing,
fails, times out or returns unusable drafts, the deterministic result stays and
the warnings are shown.

### Grounding rules

Titles, descriptions, captions, keywords and thumbnail text are assembled from:

- the final clip transcript, respecting the applied editorial timeline order;
- the clip hook and candidate title;
- the user's topic, subtopics, genre and profile settings;
- profile brand terms and CTA text.

The LLM prompt forbids invented names, places, statistics, quotations, promises,
product claims, trend metrics and words such as "viral" or "guaranteed". Drafts
are rejected when they introduce an unsupported proper noun or number, when a
restricted claim appears, or when a field exceeds its platform limit. Rejection
falls back to the deterministic draft instead of publishing the bad field.

### Platform profiles

Limits live in `apps/worker/src/clipforge/platforms.py`:
`PACKAGING_PROFILES` plus `packaging_profile()` and environment overrides via
`CLIPFORGE_PACKAGING_PROFILES_JSON`. Every entry records `sources`, `checked_at`
and a note.

| Profile | Title max / recommended | Caption max | Hashtags max / shown | Provenance |
| --- | --- | --- | --- | --- |
| TikTok | 100 / 60 | 2,200 UTF-16 code units | 30 / 5 | Direct Post API reference and TikTok help. TikTok has no separate public video title in that API; the title is internal review and thumbnail copy. |
| YouTube Shorts | 100 / 70 | 5,000 characters | 60 / 3 | YouTube help `answer/57407`, `answer/6390658`, Data API videos. More than 15 hashtags causes all to be ignored; only the first three are shown above the title. |
| Instagram Reels | 100 / 70 | 2,200 characters (conservative) | 30 / 5 | Instagram content-publishing docs and help centre. The official page fetched during the audit did not expose a verifiable numeric caption limit, so this value is documented configuration with provenance, not a confirmed platform constant. |
| Cross-platform | 100 / 60 | 2,200 characters | 30 / 5 | Conservative intersection of the supported platforms. |

The limits are configuration, not a promise. Refresh the source page and
`checked_at` when a platform changes its rules.

## 3. Trend Check providers

### Provider matrix

| Platform | Status | What is actually available |
| --- | --- | --- |
| YouTube / YouTube Shorts | Official, credential-gated | YouTube Data API v3 `search.list` + `videos.list` returns related public videos, cumulative view counts and publication dates. It is not a live Shorts trend feed and does not expose acceleration or engagement rate. Requires `CLIPFORGE_YOUTUBE_API_KEY`. |
| TikTok | Manual research | No general official TikTok trend endpoint was verified. TikTok Research Tools require approval and are region-restricted; they are not presented as a general trend API. No restricted scraping is used. |
| Instagram Reels | Manual research | Meta's official publishing APIs expose media publishing, not a general Reels trend feed. No trend endpoint was verified. |
| Cross-platform | Composite, honest | When YouTube is configured, the result is YouTube data plus an explicit note that TikTok and Instagram require manual research. It is not a combined cross-platform trend index. |

A common provider protocol and registry allow future official or authorised
providers. Provider capabilities are exposed through
`GET /api/projects/{projectId}/packaging/config`, so the UI can show what is
missing instead of pretending a feed exists.

### YouTube provider behaviour

- Query terms come from topic, subtopics, niche, title and at most the first
  part of the transcript; the whole transcript is never sent as the query.
- `regionCode` and `relevanceLanguage` come from the request, falling back to
  `US` and `en`.
- `order=viewCount`, `videoDuration=short` and `publishedAfter` use the requested
  freshness window.
- `ranking` is the provider result order. `volume` is the cumulative view count
  only when Google supplied a numeric value. `growth` is always `None`.
- `direction` is `unknown`; `evidence` and limitations say popularity only.
- HTTP errors are mapped to `quota_exhausted`, `rate_limited`, `auth_missing` or
  `unavailable`. No result is shown for a failed request.
- The request timeout defaults to 12 seconds and is configurable with
  `CLIPFORGE_TREND_TIMEOUT_SECONDS`.

## 4. Transparent ranking

`apps/worker/src/clipforge/packaging/trend/ranking.py` stores every component on
the recommendation, so the UI can explain both positives and penalties.

Default weights:

| Component | Weight |
| --- | --- |
| Semantic relevance | 0.26 |
| Platform relevance | 0.12 |
| Regional relevance | 0.10 |
| Recency | 0.12 |
| Rising interest | 0.14 |
| Content suitability | 0.14 |
| Misleading risk (penalty) | 0.07 |
| Rights constraint (penalty) | 0.05 |

Weights are clamped, normalised to 1 and overridable per request. The status
thresholds are:

- `strong_match`: semantic relevance at least 0.15 and weighted score at least
  0.68;
- `relevant_adjacent`: at least 0.52;
- `broad`: at least 0.34;
- `not_recommended`: semantic relevance below 0.15 or weighted score below
  0.34.

Every result carries `status_reason` and an `adaptation` sentence. A high score
is a fit estimate, not a prediction of views, retention or virality.

Manual references enter the same ranker with `manual=true`, provider `manual`,
no metrics, `freshness=unknown`, low confidence and explicit limitations. The
user is responsible for checking the source and rights.

## 5. Cache and provider states

`TrendService` uses the additive `trend_cache` table, keyed by provider,
platform and normalised query. Defaults:

- cache TTL: 21,600 seconds (6 hours);
- stale fallback window: 604,800 seconds (7 days);
- `CLIPFORGE_TREND_CACHE_TTL_SECONDS` and
  `CLIPFORGE_TREND_STALE_TTL_SECONDS` override them.

States the UI can display:

`live`, `cached`, `stale`, `unavailable`, `quota_exhausted`, `auth_missing`,
`rate_limited`, `manual_required`, `no_results`, `disabled`.

A fresh cached result is returned without a provider call. If the provider then
fails, the last successful cached result may be shown as `stale` with its
original timestamp and a warning; it is never presented as live. A failed
provider with no cache returns the failure state and an empty result list. A
`force` check bypasses the fresh cache but still obeys provider errors.

## 6. API and UI

Worker endpoints:

- `GET /api/projects/{projectId}/packaging/config`
- `GET /api/projects/{projectId}/candidates/{candidateId}/packaging`
- `POST /api/projects/{projectId}/candidates/{candidateId}/packaging`
- `PATCH /api/projects/{projectId}/candidates/{candidateId}/packaging`
- `POST /api/projects/{projectId}/candidates/{candidateId}/packaging/trend-check`
- `GET/PATCH /api/projects/{projectId}/settings` (existing project settings)

The browser talks only to the Next.js app, which proxies to the worker. Provider
keys and `CLIPFORGE_*` secrets remain server-side and never reach the web client.

`packaging-panel.tsx` in the clip editor provides:

- AI Packaging: generate, edit, copy, add/remove hashtags and keywords, choose a
  platform, pick a title, save the current metadata, and save selected fields as
  project defaults.
- Trend Check: topic/subtopics, platform, region, audience, niche, language,
  freshness, force refresh, weighted recommendations, provider state, manual
  references, and use/dismiss/save actions.
- Warnings for insufficient metadata, non-applied editorial timelines, provider
  failures and trend edits.

The panel keeps unsaved edits in local draft state when the parent candidate
object refreshes.

## 7. Profiles, precedence and persistence

`PackagingPreferences` on a client profile stores tone, audience, default
platform, language, region, brand voice, hashtag style/count, CTA preference and
text, trend-check mode, restricted words/topics/claims and brand terms. Older
profiles that predate this section resolve the current defaults instead of an
empty new section.

Precedence is deep-merged: request > project override > profile > default.
Profile values are snapshotted into the project so later profile edits do not
silently rewrite reviewed metadata. Project overrides still win.

Persistence is additive:

- `candidates.packaging_json` stores the complete packaging document.
- `trend_cache` stores provider-aware cached results with expiry.
- `packaging` is serialised on the candidate response and optionally in the
  shared schema.
- `PATCH` updates metadata only. It does not transcribe, re-render, change
  `start_ms`/`end_ms`, or create a render row.

## 8. Tests and mocks

`apps/worker/tests/test_packaging.py` covers:

- deterministic metadata grounded in the clip, provenance and title styles;
- platform limits for TikTok, YouTube Shorts, Instagram Reels and cross-platform,
  including TikTok's UTF-16 caption measurement;
- hashtag deduplication, relevance, categories and style/count controls;
- keyword/hashtag separation and unverified labelling;
- applied timeline order and chronological fallback with a warning;
- rejection of invented numbers and restricted claims;
- LLM rejection of an invented proper noun and statistic;
- LLM unavailable fallback and a grounded accepted draft;
- ranking statuses, explanations, weights and missing metrics;
- YouTube auth-missing, quota, popularity-only and missing-statistics states;
- fresh cache, provider-scoped cache, stale fallback and no-cache failure;
- manual reference labelling, merging and duplicate prevention across merges;
- empty topic queries falling back to transcript terms;
- default registry capability reporting with duplicate providers collapsed;
- profile defaults/overrides and old-profile compatibility;
- metadata edits preserving media and creating no render;
- API config, generate, patch and automatic trend-mode wiring;
- complete-question titles keeping their payoff up to the hard limit;
- editorial `context`/`development`/`payoff` roles surviving the render spec.

The network is mocked. Provider tests inject a fake fetch callable, trend tests
inject a fake provider and an in-memory cache, and the LLM tests inject a fake
`complete_json`. No test calls YouTube, TikTok, Instagram or an LLM.

The worker suite passes for this change set (187 tests, 39 in
`test_packaging.py`) and `ruff check` passes. TikTok caption
length is validated in UTF-16 code units, matching the platform's field
definition; the other configured platforms count characters. The API tests also
cover the profile `automatic` trend mode, which runs a check without requiring
the per-request flag.

## 9. Verified sources and what still needs approval

Verified directly during this audit:

- TikTok Content Posting API direct post: title limit of 2,200 UTF-16 code
  units, plus the metadata field shape.
- YouTube help: upload defaults, hashtags (up to 60, more than 15 ignored,
  first three shown) and the Data API video resource.
- YouTube Data API v3 search/videos endpoints for related public-video data.
- Instagram Graph content-publishing documentation for publishing media, not a
  trend feed.

Not verified or not available:

- A general official TikTok trend endpoint. TikTok Research Tools require
  approval and are region-restricted.
- TikTok Discovery documentation; the URL tried during the audit returned 404.
- A general Instagram Reels trend endpoint.
- An Instagram numeric caption limit from a successfully fetched official page;
  the configured value is conservative and marked accordingly.
- A live YouTube Shorts trend feed, acceleration or engagement rate.
- Any paid third-party trend provider. None is required.
- Real provider responses in this environment because no credentials were
  configured. The provider path is implemented and mocked, but Trend Check
  cannot be described as live until a key is set and a real response is checked.

## 10. Remaining limitations

- No keyword search-volume validation exists. Keywords are labelled
  AI-generated unless a future verified source supplies data.
- Trend ranking is lexical/weighted, not an embedding model. It explains overlap
  honestly but can miss synonyms.
- The YouTube provider uses search popularity, not a trend acceleration series.
  The UI must keep that distinction visible.
- Manual references carry no metrics by design.
- Selecting a trend adds at most one evidence-labelled hashtag; it does not
  rewrite the caption or description around the trend.
- Browser verification in this environment is HTTP/API/build-based; no
  Playwright MCP was available.
- A live Render/Supabase deployment was not exercised. Trend credentials,
  quotas and provider behaviour must be rechecked in the target deployment.
