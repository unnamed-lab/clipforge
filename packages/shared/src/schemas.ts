import { z } from "zod";

/** Wire contracts shared by the Next.js app and the Python worker.
 *
 * The Python service mirrors these shapes in `clipforge/api/contracts.py`, and
 * `packages/shared/fixtures/*.json` is exercised by both test suites so the two
 * runtimes cannot drift silently.
 */

export const jobStateSchema = z.enum(["queued", "running", "completed", "failed", "cancelled"]);
export type JobState = z.infer<typeof jobStateSchema>;

export const jobSchema = z
  .object({
    id: z.string(),
    project_id: z.string().nullable(),
    type: z.string(),
    state: jobStateSchema,
    stage: z.string(),
    progress: z.number(),
    message: z.string(),
    error_code: z.string().nullable(),
    error_message: z.string().nullable(),
    attempts: z.number(),
    payload: z.record(z.string(), z.unknown()).default({}),
    result: z.record(z.string(), z.unknown()).nullable(),
    cancel_requested: z.boolean(),
    created_at: z.string(),
    started_at: z.string().nullable(),
    finished_at: z.string().nullable(),
  })
  .passthrough();
export type Job = z.infer<typeof jobSchema>;

export const projectSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    status: z.string(),
    goals: z.record(z.string(), z.unknown()).default({}),
    settings: z.record(z.string(), z.unknown()).default({}),
    created_at: z.string(),
    updated_at: z.string(),
    duration_ms: z.number().nullable().optional(),
    width: z.number().nullable().optional(),
    height: z.number().nullable().optional(),
    has_audio: z.boolean().nullable().optional(),
    original_name: z.string().nullable().optional(),
    candidate_count: z.number().optional(),
    render_count: z.number().optional(),
  })
  .passthrough();
export type Project = z.infer<typeof projectSchema>;

export const sourceSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    kind: z.enum(["upload", "url", "sample"]),
    original_name: z.string(),
    relative_path: z.string(),
    size_bytes: z.number(),
    duration_ms: z.number(),
    width: z.number().nullable(),
    height: z.number().nullable(),
    fps: z.number().nullable(),
    has_audio: z.boolean(),
    video_codec: z.string().nullable(),
    audio_codec: z.string().nullable(),
    container: z.string().nullable(),
    source_url: z.string().nullable(),
    created_at: z.string(),
  })
  .passthrough();
export type Source = z.infer<typeof sourceSchema>;

export const scoreComponentSchema = z.object({
  score: z.number(),
  reason: z.string(),
});
export type ScoreComponent = z.infer<typeof scoreComponentSchema>;

export const candidateScoresSchema = z
  .object({
    overall: z.number(),
    components: z.record(z.string(), scoreComponentSchema),
    weights: z.record(z.string(), z.number()).default({}),
    method: z.string().default("baseline"),
    method_version: z.string().default("baseline-1"),
    estimated: z.boolean().default(true),
    note: z.string().optional(),
  })
  .passthrough();
export type CandidateScores = z.infer<typeof candidateScoresSchema>;

export const candidateBoundarySchema = z
  .object({
    version: z.number().optional(),
    suspicious: z.boolean().default(false),
    reviewed: z.enum(["pending", "accepted", "rejected"]).default("pending"),
    suspicion: z.number().optional(),
    start_ms: z.number().optional(),
    end_ms: z.number().optional(),
    accepted_end_ms: z.number().nullable().optional(),
    current_sentence: z.string().nullable().optional(),
    next_sentence: z.string().nullable().optional(),
    last_sentence_ends_with_terminator: z.boolean().optional(),
    nearest_cut_before_ms: z.number().nullable().optional(),
    nearest_cut_after_ms: z.number().nullable().optional(),
    evidence: z.array(z.string()).default([]),
    alternatives: z
      .array(
        z.object({
          end_ms: z.number(),
          label: z.string(),
          reason: z.string(),
          confidence: z.number(),
          source: z.string(),
        }),
      )
      .default([]),
  })
  .passthrough();
export type CandidateBoundary = z.infer<typeof candidateBoundarySchema>;

export const candidateDurationSchema = z
  .object({
    preset: z.enum(["short", "standard", "extended", "custom"]).optional(),
    label: z.string().optional(),
    min_ms: z.number().optional(),
    max_ms: z.number().optional(),
    target_ms: z.number().optional(),
    actual_ms: z.number().optional(),
    status: z.enum(["fit", "near", "too_short", "too_long", "unknown"]).optional(),
    reason: z.string().optional(),
    proximity: z.number().optional(),
  })
  .passthrough();
export type CandidateDuration = z.infer<typeof candidateDurationSchema>;

export const editorialAlternativeSchema = z
  .object({
    id: z.string(),
    label: z.string(),
    strategy: z.string(),
    description: z.string().default(""),
    available: z.boolean().default(true),
    confidence: z.number().default(0),
    estimated_duration_ms: z.number().default(0),
    pacing_profile: z.string().nullable().optional(),
    reason: z.string().default(""),
    requires_approval: z.boolean().default(false),
  })
  .passthrough();

export const editorialDecisionSchema = z
  .object({
    version: z.number().optional(),
    strategy: z.string().default("chronological"),
    label: z.string().default("Chronological"),
    style: z.string().default("automatic"),
    rationale: z.string().default(""),
    confidence: z.number().default(0),
    alternatives: z.array(editorialAlternativeSchema).default([]),
    concerns: z.array(z.string()).default([]),
    review_required: z.boolean().default(false),
    reordered: z.boolean().default(false),
    applied: z.boolean().default(false),
    fallback: z.string().default(""),
    pacing_profile: z.string().default("balanced"),
    timeline: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .passthrough();
export type EditorialDecision = z.infer<typeof editorialDecisionSchema>;

export const candidateEditorialSchema = z
  .object({
    version: z.number().optional(),
    profile: z.string().optional(),
    strategy: z.string().optional(),
    rationale: z.string().optional(),
    confidence: z.number().optional(),
    context_loss: z.number().optional(),
    review_required: z.boolean().optional(),
    warnings: z.array(z.string()).default([]),
    reordered: z.boolean().optional(),
    segments: z.array(z.record(z.string(), z.unknown())).default([]),
    alternatives: z.array(editorialAlternativeSchema).default([]),
    timeline: z.record(z.string(), z.unknown()).optional(),
    editorial: editorialDecisionSchema.optional(),
  })
  .passthrough();
export type CandidateEditorial = z.infer<typeof candidateEditorialSchema>;

export const packagingTitleSchema = z
  .object({
    text: z.string(),
    style: z.enum(["direct", "curiosity", "question", "result", "how_to", "quote"]).default("direct"),
    reason: z.string().default(""),
    chars: z.number().default(0),
    limit: z.number().default(0),
    within_limit: z.boolean().default(true),
    basis: z.enum(["transcript", "hook", "topic", "profile"]).default("transcript"),
  })
  .passthrough();
export type PackagingTitle = z.infer<typeof packagingTitleSchema>;

export const packagingHashtagSchema = z
  .object({
    tag: z.string(),
    category: z.enum(["topic", "niche", "broad", "trending"]).default("topic"),
    reason: z.string().default(""),
    provenance: z.enum(["transcript", "profile", "genre", "trend", "platform"]).default("transcript"),
    relevance: z.number().default(0.5),
  })
  .passthrough();
export type PackagingHashtag = z.infer<typeof packagingHashtagSchema>;

export const packagingKeywordSchema = z
  .object({
    phrase: z.string(),
    reason: z.string().default(""),
    source: z.enum(["ai_generated", "platform_verified"]).default("ai_generated"),
    verified: z.boolean().default(false),
  })
  .passthrough();
export type PackagingKeyword = z.infer<typeof packagingKeywordSchema>;

export const packagingProvenanceSchema = z
  .object({
    generated_at: z.string().default(""),
    method: z.enum(["deterministic", "llm", "manual", "hybrid"]).default("deterministic"),
    provider: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
    transcript_id: z.string().nullable().optional(),
    source_start_ms: z.number().default(0),
    source_end_ms: z.number().default(0),
    timeline_applied: z.boolean().default(false),
    sentence_ids: z.array(z.number()).default([]),
    grounding: z.array(z.string()).default([]),
    warnings: z.array(z.string()).default([]),
  })
  .passthrough();
export type PackagingProvenance = z.infer<typeof packagingProvenanceSchema>;

export const clipMetadataSchema = z
  .object({
    titles: z.array(packagingTitleSchema).default([]),
    descriptions: z
      .object({
        short: z.string().default(""),
        long: z.string().default(""),
        short_limit: z.number().default(0),
        long_limit: z.number().default(0),
        within_limit: z.boolean().default(true),
      })
      .passthrough()
      .default({}),
    caption: z
      .object({
        platform: z.string().default("cross_platform"),
        label: z.string().default(""),
        text: z.string().default(""),
        char_count: z.number().default(0),
        limit: z.number().default(0),
        within_limit: z.boolean().default(true),
        notes: z.array(z.string()).default([]),
      })
      .passthrough()
      .default({}),
    platform_captions: z
      .array(
        z
          .object({
            platform: z.string(),
            label: z.string().default(""),
            text: z.string().default(""),
            char_count: z.number().default(0),
            limit: z.number().default(0),
            within_limit: z.boolean().default(true),
            notes: z.array(z.string()).default([]),
          })
          .passthrough(),
      )
      .default([]),
    hashtags: z.array(packagingHashtagSchema).default([]),
    keywords: z.array(packagingKeywordSchema).default([]),
    cta: z.string().nullable().optional(),
    thumbnail_text: z
      .object({
        text: z.string().default(""),
        limit: z.number().default(0),
        within_limit: z.boolean().default(true),
        reason: z.string().default(""),
      })
      .passthrough()
      .nullable()
      .optional(),
    validation: z
      .object({
        ok: z.boolean().default(true),
        issues: z.array(z.string()).default([]),
        warnings: z.array(z.string()).default([]),
        platform: z.string().default("cross_platform"),
      })
      .passthrough()
      .default({}),
    provenance: packagingProvenanceSchema.default({}),
  })
  .passthrough();
export type ClipMetadata = z.infer<typeof clipMetadataSchema>;

export const trendScoreBreakdownSchema = z
  .object({
    semantic_relevance: z.number().default(0),
    platform_relevance: z.number().default(0),
    regional_relevance: z.number().default(0),
    recency: z.number().default(0),
    rising_interest: z.number().default(0),
    content_suitability: z.number().default(0),
    misleading_risk: z.number().default(0),
    rights_constraint: z.number().default(0),
    weighted: z.number().default(0),
  })
  .passthrough();
export type TrendScoreBreakdown = z.infer<typeof trendScoreBreakdownSchema>;

export const trendRecommendationSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    platform: z.string().default("unknown"),
    provider: z.string().default("manual"),
    region: z.string().default(""),
    category: z.string().default(""),
    evidence: z.string().default(""),
    volume: z.number().nullable().optional(),
    ranking: z.number().nullable().optional(),
    growth: z.number().nullable().optional(),
    observed_at: z.string().nullable().optional(),
    updated_at: z.string().nullable().optional(),
    published_at: z.string().nullable().optional(),
    direction: z.enum(["rising", "flat", "falling", "unknown"]).default("unknown"),
    source_url: z.string().nullable().optional(),
    freshness: z.enum(["fresh", "recent", "stale", "unknown"]).default("unknown"),
    confidence: z.enum(["low", "medium", "high"]).default("low"),
    limitations: z.array(z.string()).default([]),
    rights: z.string().default(""),
    status: z.enum(["strong_match", "relevant_adjacent", "broad", "not_recommended"]).default("not_recommended"),
    status_reason: z.string().default(""),
    adaptation: z.string().default(""),
    scores: trendScoreBreakdownSchema.default({}),
    used: z.boolean().default(false),
    dismissed: z.boolean().default(false),
    manual: z.boolean().default(false),
  })
  .passthrough();
export type TrendRecommendation = z.infer<typeof trendRecommendationSchema>;

export const trendCheckResultSchema = z
  .object({
    status: z
      .enum([
        "live",
        "cached",
        "stale",
        "unavailable",
        "quota_exhausted",
        "auth_missing",
        "rate_limited",
        "manual_required",
        "no_results",
        "disabled",
      ])
      .default("unavailable"),
    provider: z.string().default("none"),
    platform: z.string().default("cross_platform"),
    checked_at: z.string().default(""),
    expires_at: z.string().nullable().optional(),
    cached: z.boolean().default(false),
    stale: z.boolean().default(false),
    settings: z.record(z.string(), z.unknown()).default({}),
    results: z.array(trendRecommendationSchema).default([]),
    notes: z.array(z.string()).default([]),
    limitations: z.array(z.string()).default([]),
    error_code: z.string().nullable().optional(),
    error_message: z.string().nullable().optional(),
    manual_references: z.array(trendRecommendationSchema).default([]),
  })
  .passthrough();
export type TrendCheckResult = z.infer<typeof trendCheckResultSchema>;

export const packagingDocumentSchema = z
  .object({
    version: z.number().default(1),
    platform: z.string().default("cross_platform"),
    metadata: clipMetadataSchema.nullable().optional(),
    trend: trendCheckResultSchema.nullable().optional(),
    settings: z.record(z.string(), z.unknown()).default({}),
    dismissed_trend_ids: z.array(z.string()).default([]),
    saved_trends: z.array(trendRecommendationSchema).default([]),
    manual_trends: z.array(z.record(z.string(), z.unknown())).default([]),
    updated_at: z.string().default(""),
    warnings: z.array(z.string()).default([]),
  })
  .passthrough();
export type PackagingDocument = z.infer<typeof packagingDocumentSchema>;

export const packagingConfigSchema = z
  .object({
    settings: z.record(z.string(), z.unknown()).default({}),
    provenance: z.record(z.string(), z.string()).default({}),
    platforms: z.record(z.string(), z.record(z.string(), z.unknown())).default({}),
    providers: z.record(z.string(), z.unknown()).default({}),
    trend_weights: z.record(z.string(), z.number()).default({}),
    notes: z.array(z.string()).default([]),
  })
  .passthrough();
export type PackagingConfig = z.infer<typeof packagingConfigSchema>;

export const candidateSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    analysis_run_id: z.string(),
    rank: z.number(),
    start_ms: z.number(),
    end_ms: z.number(),
    duration_ms: z.number(),
    title: z.string(),
    hook: z.string(),
    reason: z.string(),
    category: z.string(),
    excerpt: z.string(),
    scores: candidateScoresSchema,
    overall: z.number(),
    warnings: z.array(z.string()),
    method: z.string(),
    method_version: z.string(),
    decision: z.enum(["proposed", "accepted", "rejected"]),
    user_start_ms: z.number().nullable(),
    user_end_ms: z.number().nullable(),
    duplicate_of: z.string().nullable().optional(),
    edited: z.boolean(),
    boundary: candidateBoundarySchema.optional(),
    duration: candidateDurationSchema.optional(),
    editorial: candidateEditorialSchema.optional(),
    packaging: packagingDocumentSchema.optional(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .passthrough();
export type Candidate = z.infer<typeof candidateSchema>;

export const reviewSchema = z
  .object({
    version: z.number().optional(),
    status: z.enum(["pass", "warn", "fail"]).optional(),
    blocking: z.boolean().optional(),
    summary: z.string().optional(),
    checks: z
      .array(
        z
          .object({
            id: z.string(),
            label: z.string(),
            status: z.enum(["pass", "warn", "fail"]),
            detail: z.string(),
          })
          .passthrough(),
      )
      .default([]),
    edl: z.record(z.string(), z.unknown()).optional(),
    output: z.record(z.string(), z.unknown()).optional(),
    draft_relative_path: z.string().optional(),
    draft_error: z.string().optional(),
  })
  .passthrough();
export type Review = z.infer<typeof reviewSchema>;

export const renderSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    job_id: z.string().nullable(),
    candidate_id: z.string().nullable(),
    spec: z.record(z.string(), z.unknown()),
    status: z.enum(["queued", "running", "completed", "failed", "cancelled"]),
    size_bytes: z.number().nullable(),
    duration_ms: z.number().nullable(),
    width: z.number().nullable(),
    height: z.number().nullable(),
    error_code: z.string().nullable(),
    error_message: z.string().nullable(),
    download_url: z.string().nullable(),
    review: reviewSchema.default({ checks: [] }),
    created_at: z.string(),
    finished_at: z.string().nullable(),
  })
  .passthrough();
export type Render = z.infer<typeof renderSchema>;

export const transcriptWordSchema = z.object({
  start_ms: z.number(),
  end_ms: z.number(),
  word: z.string(),
  probability: z.number().nullable().optional(),
});

export const transcriptSegmentSchema = z
  .object({
    id: z.number(),
    start_ms: z.number(),
    end_ms: z.number(),
    text: z.string(),
    words: z.array(transcriptWordSchema).default([]),
    avg_logprob: z.number().nullable().optional(),
    no_speech_prob: z.number().nullable().optional(),
    speaker: z.string().nullable().optional(),
  })
  .passthrough();
export type TranscriptSegment = z.infer<typeof transcriptSegmentSchema>;

export const transcriptSchema = z
  .object({
    id: z.string(),
    language: z.string().nullable(),
    language_probability: z.number().nullable(),
    model: z.string(),
    duration_ms: z.number(),
    created_at: z.string(),
    segments: z.array(transcriptSegmentSchema),
  })
  .passthrough();
export type Transcript = z.infer<typeof transcriptSchema>;

export const analysisRunSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    status: z.string(),
    settings: z.record(z.string(), z.unknown()).default({}),
    provider: z.string().nullable(),
    warnings: z.array(z.string()).default([]),
    created_at: z.string(),
    finished_at: z.string().nullable(),
  })
  .passthrough();
export type AnalysisRun = z.infer<typeof analysisRunSchema>;

export const projectDetailSchema = z.object({
  project: projectSchema,
  source: sourceSchema.nullable(),
  transcript: z
    .object({
      id: z.string(),
      language: z.string().nullable(),
      language_probability: z.number().nullable(),
      model: z.string(),
      segment_count: z.number(),
      word_count: z.number(),
      created_at: z.string(),
    })
    .nullable(),
  analysis: analysisRunSchema.nullable(),
  candidates: z.array(candidateSchema),
  renders: z.array(renderSchema),
  jobs: z.array(jobSchema),
});
export type ProjectDetail = z.infer<typeof projectDetailSchema>;

export const capabilitiesSchema = z
  .object({
    version: z.string(),
    media: z.object({
      ffmpeg: z.object({ available: z.boolean(), version: z.string().nullable() }),
      ffprobe: z.object({ available: z.boolean(), version: z.string().nullable() }),
      hwaccel: z.object({ encoders: z.array(z.string()), acceleration_for_transcription: z.boolean() }),
    }),
    transcription: z.object({
      provider: z.string(),
      available: z.boolean(),
      detail: z.string().nullable(),
      device: z.string(),
      compute_type: z.string(),
      default_model: z.string(),
      models: z.array(z.object({ id: z.string(), download_mb: z.number() })),
    }),
    diarisation: z.object({ available: z.boolean(), provider: z.string(), detail: z.string() }),
    llm: z.object({
      enabled: z.boolean(),
      configured: z.boolean(),
      model: z.string().nullable(),
      requires_consent: z.boolean(),
    }),
    storage: z.object({ free_bytes: z.number(), min_free_bytes: z.number(), data_dir: z.string() }),
    limits: z.object({ max_upload_bytes: z.number(), max_duration_ms: z.number() }),
    platform: z.object({ os: z.string(), release: z.string(), cpu_count: z.number(), python: z.string() }),
  })
  .passthrough();
export type Capabilities = z.infer<typeof capabilitiesSchema>;

/** Render request body accepted by POST /api/projects/{id}/renders. */
export const compositionModeSchema = z.enum(["auto", "crop", "fit", "pip", "split", "manual"]);
export type CompositionMode = z.infer<typeof compositionModeSchema>;

export const renderRequestSchema = z.object({
  candidate_id: z.string().optional(),
  segments: z
    .array(z.object({ start_ms: z.number(), end_ms: z.number(), composition: z.record(z.string(), z.unknown()).optional() }))
    .optional(),
  aspect: z.enum(["9:16", "16:9", "1:1"]),
  resolution: z.union([z.literal(720), z.literal(1080)]),
  framing: z.enum(["crop", "blur"]),
  composition: compositionModeSchema.default("auto"),
  composition_overrides: z.record(z.string(), z.record(z.string(), z.unknown())).optional(),
  crop_x_pct: z.number().min(0).max(100).optional(),
  crop_y_pct: z.number().min(0).max(100).optional(),
  caption_preset: z.enum(["clean", "large", "minimal", "branded", "custom"]),
  captions_enabled: z.boolean(),
  captions: z
    .object({
      font_family: z.string(),
      font_size: z.number(),
      bold: z.boolean(),
      position: z.enum(["bottom", "center", "top", "custom"]),
      position_pct: z.number(),
      alignment: z.enum(["left", "center", "right"]),
      margin_v: z.number(),
      margin_h: z.number(),
      max_line_width_pct: z.number(),
      min_font_scale: z.number(),
      avoid_visual_overlap: z.boolean(),
      primary_color: z.string(),
      highlight_color: z.string(),
      outline_color: z.string(),
      outline_width: z.number(),
      uppercase: z.boolean(),
      max_words_per_line: z.number(),
      max_chars_per_line: z.number(),
      max_lines: z.number(),
      background: z.boolean(),
    })
    .partial()
    .optional(),
  normalize_audio: z.boolean().optional(),
  title: z.string().max(120).optional(),
});
export type RenderRequest = z.infer<typeof renderRequestSchema>;

export const analysisRequestSchema = z.object({
  goal: z.string(),
  clip_count: z.number().min(3).max(20),
  duration_band: z.enum(["auto", "short", "medium", "long"]),
  quality: z.enum(["fast", "balanced", "best"]),
  language: z.string().max(12).nullable().optional(),
  use_llm: z.boolean().optional(),
  llm_consent: z.boolean().optional(),
});
export type AnalysisRequest = z.infer<typeof analysisRequestSchema>;

export const apiErrorSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
    details: z.record(z.string(), z.unknown()).optional(),
  }),
});
export type ApiError = z.infer<typeof apiErrorSchema>;
