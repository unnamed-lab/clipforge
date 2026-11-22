import { z } from "zod";

/** Wire contracts shared by the Next.js app and the Python worker.
 *
 * The Python service mirrors these shapes in `clipforge/api/contracts.py`, and
 * `packages/shared/fixtures/*.json` is exercised by both test suites so the two
 * runtimes cannot drift silently.
 */

export const jobStateSchema = z.enum(["queued", "running", "waiting", "completed", "failed", "cancelled"]);
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
    /** Measured download/prepare facts, separate from progress text. */
    metrics: z.record(z.string(), z.unknown()).default({}),
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

/** What a source was downloaded at, and whether that matched the request. */
export const downloadSummarySchema = z
  .object({
    quality: z.string(),
    requested_quality: z.string().nullable().optional(),
    selector: z.string(),
    width: z.number().nullable().optional(),
    height: z.number().nullable().optional(),
    fps: z.number().nullable().optional(),
    video_codec: z.string().nullable().optional(),
    audio_codec: z.string().nullable().optional(),
    container: z.string().nullable().optional(),
    has_video: z.boolean().optional(),
    estimated_bytes: z.number().nullable().optional(),
    approximate: z.boolean().default(true),
    bytes_downloaded: z.number().optional(),
    size_bytes: z.number().optional(),
    elapsed_seconds: z.number().optional(),
    speed_bps: z.number().nullable().optional(),
    explanation: z.string().default(""),
    warnings: z.array(z.string()).default([]),
    processing: z.record(z.string(), z.unknown()).default({}),
    video_format_id: z.string().nullable().optional(),
    audio_format_id: z.string().nullable().optional(),
    reused: z.boolean().optional(),
  })
  .passthrough();
export type DownloadSummary = z.infer<typeof downloadSummarySchema>;

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
    has_video: z.boolean().default(true),
    video_codec: z.string().nullable(),
    audio_codec: z.string().nullable(),
    container: z.string().nullable(),
    source_url: z.string().nullable(),
    download: downloadSummarySchema.nullable().optional(),
    created_at: z.string(),
  })
  .passthrough();
export type Source = z.infer<typeof sourceSchema>;

/** A derived working copy. Never an export input: exports read the source. */
export const mediaAssetSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    source_id: z.string(),
    role: z.enum(["audio", "proxy"]),
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
    settings: z.record(z.string(), z.unknown()).default({}),
    source_identity: z.string().nullable().optional(),
    created_at: z.string(),
  })
  .passthrough();
export type MediaAsset = z.infer<typeof mediaAssetSchema>;

export const downloadQualitySchema = z.enum([
  "lowest",
  "144p",
  "240p",
  "360p",
  "480p",
  "720p",
  "1080p",
  "1440p",
  "2160p",
  "best",
  "audio",
]);
export type DownloadQuality = z.infer<typeof downloadQualitySchema>;

/**
 * Kept in step with the worker's ladder so the custom control is usable before
 * the capabilities request answers (and when the worker is offline).
 */
export const DOWNLOAD_QUALITY_LADDER: ReadonlyArray<{ id: DownloadQuality; label: string }> = [
  { id: "lowest", label: "Lowest available" },
  { id: "144p", label: "144p" },
  { id: "240p", label: "240p" },
  { id: "360p", label: "360p" },
  { id: "480p", label: "480p" },
  { id: "720p", label: "720p" },
  { id: "1080p", label: "1080p" },
  { id: "1440p", label: "1440p / 2K" },
  { id: "2160p", label: "2160p / 4K" },
  { id: "best", label: "Best available" },
  { id: "audio", label: "Audio only" },
];

export const processingModeSchema = z.enum(["fast", "balanced", "high", "custom"]);
export type ProcessingMode = z.infer<typeof processingModeSchema>;

export const urlImportRequestSchema = z.object({
  url: z.string().min(8).max(2048),
  rights_confirmed: z.boolean(),
  quality: downloadQualitySchema.optional(),
  processing: processingModeSchema.default("balanced"),
  proxy_height: z.number().int().min(144).max(1440).optional(),
});
export type UrlImportRequest = z.infer<typeof urlImportRequestSchema>;

export const urlInspectResultSchema = z
  .object({
    url: z.string(),
    host: z.string(),
    title: z.string(),
    uploader: z.string().nullable().optional(),
    duration_ms: z.number(),
    thumbnail: z.string().nullable().optional(),
    qualities: z.array(
      z
        .object({
          id: z.string(),
          label: z.string(),
          max_height: z.number().nullable().optional(),
          available: z.boolean(),
          closest_height: z.number().nullable().optional(),
          fallback: z.boolean().default(false),
          estimated_bytes: z.number().nullable().optional(),
          approximate: z.boolean().default(true),
        })
        .passthrough(),
    ),
    selection: z.record(z.string(), z.unknown()),
    plan: z.record(z.string(), z.unknown()),
    warnings: z.array(z.string()).default([]),
    approximate_sizes: z.boolean().default(true),
  })
  .passthrough();
export type UrlInspectResult = z.infer<typeof urlInspectResultSchema>;

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
    /** What the picture evidence came from; 144p frames are not a close read. */
    analysis: z
      .object({
        sampled_width: z.number().nullable().optional(),
        sampled_height: z.number().nullable().optional(),
        scene_fps: z.number().nullable().optional(),
        source_height: z.number().nullable().optional(),
        evidence: z.enum(["detail", "low_resolution"]).optional(),
        text_verified: z.boolean().optional(),
        text_heavy: z.boolean().optional(),
      })
      .passthrough()
      .optional(),
    analysis_warnings: z.array(z.string()).default([]),
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

export const timelineEntrySchema = z
  .object({
    index: z.number().default(0),
    role: z.string().default("main"),
    source_start_ms: z.number(),
    source_end_ms: z.number(),
    output_start_ms: z.number(),
    output_end_ms: z.number(),
    sentence_start: z.number().nullable().optional(),
    sentence_end: z.number().nullable().optional(),
    transition: z.string().default("cut"),
    key_point: z.string().default(""),
    reason: z.string().default(""),
  })
  .passthrough();
export type TimelineEntry = z.infer<typeof timelineEntrySchema>;

export const timelineReportSchema = z
  .object({
    version: z.number().default(1),
    entries: z.array(timelineEntrySchema).default([]),
    total_output_ms: z.number().default(0),
    covered_ms: z.number().default(0),
    missing_ms: z.number().default(0),
    duplicated_ms: z.number().default(0),
    loop_ms: z.number().default(0),
    gaps: z.array(z.record(z.string(), z.unknown())).default([]),
    overlaps: z.array(z.record(z.string(), z.unknown())).default([]),
    valid: z.boolean().default(true),
    warnings: z.array(z.string()).default([]),
  })
  .passthrough();
export type TimelineReport = z.infer<typeof timelineReportSchema>;

export const hookDocumentSchema = z
  .object({
    text: z.string().default(""),
    start_ms: z.number().default(0),
    end_ms: z.number().default(0),
    duration_ms: z.number().default(0),
    source: z.enum(["candidate", "external", "none"]).default("none"),
    placement: z.enum(["before", "none"]).default("none"),
    direction: z.enum(["opening", "before", "after", "none"]).default("none"),
    approved: z.boolean().default(false),
    grounded: z.boolean().default(true),
    quality: z.number().default(0),
    strategy: z.string().default("story"),
    reason: z.string().default(""),
    needs_context: z.boolean().default(false),
    review_required: z.boolean().default(false),
    scores: z.record(z.string(), z.number()).default({}),
    warnings: z.array(z.string()).default([]),
    estimated: z.boolean().default(true),
  })
  .passthrough();
export type HookDocument = z.infer<typeof hookDocumentSchema>;

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
    timeline: timelineReportSchema.nullable().optional(),
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
    timeline: timelineReportSchema.optional(),
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
    angle: z
      .enum([
        "descriptive",
        "insight",
        "problem_solution",
        "question",
        "benefit",
        "contrarian",
        "story",
        "search",
        "quote",
        "curiosity",
        "result",
        "how_to",
        "direct",
      ])
      .default("direct"),
    evidence_ids: z.array(z.string()).default([]),
    limitations: z.array(z.string()).default([]),
    scores: z
      .object({
        accuracy: z.number().default(0),
        specificity: z.number().default(0),
        audience_relevance: z.number().default(0),
        curiosity: z.number().default(0),
        search_relevance: z.number().default(0),
        readability: z.number().default(0),
        weighted: z.number().default(0),
      })
      .passthrough()
      .nullable()
      .optional(),
    accepted: z.boolean().default(true),
    rejection_reason: z.string().default(""),
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

export const packagingBriefEvidenceSchema = z
  .object({
    id: z.string(),
    text: z.string().default(""),
    start_ms: z.number().default(0),
    end_ms: z.number().default(0),
    kind: z.enum(["claim", "example", "caveat", "hook", "context", "section"]).default("claim"),
    section_id: z.string().nullable().optional(),
    sentence_id: z.number().nullable().optional(),
    in_clip: z.boolean().default(true),
  })
  .passthrough();
export type PackagingBriefEvidence = z.infer<typeof packagingBriefEvidenceSchema>;

export const packagingBriefSchema = z
  .object({
    version: z.string().default(""),
    method: z.enum(["deterministic", "llm"]).default("deterministic"),
    brief_key: z.string().default(""),
    clip_type: z.string().default("unknown"),
    subject: z.string().default(""),
    video_topic: z.string().default(""),
    clip_topic: z.string().default(""),
    central_claim: z.string().default(""),
    key_insight: z.string().default(""),
    audience_problem: z.string().default(""),
    practical_takeaway: z.string().default(""),
    supporting_points: z.array(z.string()).default([]),
    example_evidence: z.array(z.string()).default([]),
    caveats: z.array(z.string()).default([]),
    stronger_hook: z.string().default(""),
    hook_evidence_id: z.string().nullable().optional(),
    entities: z.array(z.string()).default([]),
    search_concepts: z.array(z.string()).default([]),
    minor_topics: z.array(z.string()).default([]),
    wider_context: z.array(z.string()).default([]),
    evidence: z.array(packagingBriefEvidenceSchema).default([]),
    sections: z.array(z.record(z.string(), z.unknown())).default([]),
    source_title: z.string().nullable().optional(),
    source_description: z.string().nullable().optional(),
    confidence: z.enum(["high", "medium", "low"]).default("medium"),
    ambiguity: z.array(z.string()).default([]),
    warnings: z.array(z.string()).default([]),
    context_chars: z.number().default(0),
    omitted_sections: z.number().default(0),
    truncated: z.boolean().default(false),
  })
  .passthrough();
export type PackagingBrief = z.infer<typeof packagingBriefSchema>;

export const keywordObservationSchema = z
  .object({
    id: z.string(),
    term: z.string(),
    provider: z.string(),
    kind: z.enum(["suggestion", "relative_interest", "public_video", "manual", "ai_hypothesis"]).default("suggestion"),
    value: z.number().nullable().optional(),
    unit: z.string().default(""),
    region: z.string().default(""),
    language: z.string().default(""),
    retrieved_at: z.string().default(""),
    freshness: z.enum(["fresh", "recent", "stale", "unknown"]).default("unknown"),
    source_url: z.string().nullable().optional(),
    evidence: z.string().default(""),
    limitations: z.array(z.string()).default([]),
    confidence: z.enum(["low", "medium", "high"]).default("low"),
    manual: z.boolean().default(false),
  })
  .passthrough();
export type KeywordObservation = z.infer<typeof keywordObservationSchema>;

export const keywordResearchSchema = z
  .object({
    mode: z.enum(["off", "free", "enhanced"]).default("off"),
    status: z.string().default("disabled"),
    retrieved_at: z.string().default(""),
    providers: z.array(z.record(z.string(), z.unknown())).default([]),
    observations: z.array(keywordObservationSchema).default([]),
    suggested_terms: z.array(z.string()).default([]),
    terms_used: z.array(z.string()).default([]),
    notes: z.array(z.string()).default([]),
    limitations: z.array(z.string()).default([]),
    cached: z.boolean().default(false),
  })
  .passthrough();
export type KeywordResearch = z.infer<typeof keywordResearchSchema>;

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
    thumbnail_alternatives: z.array(z.string()).default([]),
    content_angle: z.string().default(""),
    key_takeaway: z.string().default(""),
    suggested_audience: z.string().default(""),
    source_hook: z.string().default(""),
    source_hook_start_ms: z.number().default(0),
    editorial_rationale: z.string().default(""),
    context_warnings: z.array(z.string()).default([]),
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
    version: z.number().default(2),
    platform: z.string().default("cross_platform"),
    metadata: clipMetadataSchema.nullable().optional(),
    brief: packagingBriefSchema.nullable().optional(),
    research: keywordResearchSchema.nullable().optional(),
    trend: trendCheckResultSchema.nullable().optional(),
    settings: z.record(z.string(), z.unknown()).default({}),
    edited_fields: z.array(z.string()).default([]),
    skill_version: z.string().default(""),
    brief_meta: z.record(z.string(), z.unknown()).default({}),
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
    research: z.record(z.string(), z.unknown()).default({}),
    skill: z.record(z.string(), z.unknown()).default({}),
    title_weights: z.record(z.string(), z.number()).default({}),
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
    moment_type: z.string().default(""),
    speaker: z.string().nullable().optional(),
    batch_kind: z.string().optional(),
    boundary: candidateBoundarySchema.optional(),
    duration: candidateDurationSchema.optional(),
    hook_detail: hookDocumentSchema.optional(),
    hook_candidates: z.array(hookDocumentSchema).default([]),
    editorial: candidateEditorialSchema.optional(),
    packaging: packagingDocumentSchema.optional(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .passthrough();
export type Candidate = z.infer<typeof candidateSchema>;

export const discoveryMomentTypeSchema = z
  .object({
    id: z.string(),
    label: z.string(),
    evidence: z.string(),
    available: z.boolean(),
  })
  .passthrough();
export type DiscoveryMomentType = z.infer<typeof discoveryMomentTypeSchema>;

export const discoveryCriteriaSchema = z.object({
  count: z.number().int().min(1),
  moment_types: z.array(z.string()).default([]),
  include_topics: z.array(z.string()).default([]),
  include_keywords: z.array(z.string()).default([]),
  include_speakers: z.array(z.string()).default([]),
  source_start_ms: z.number().nullable().optional(),
  source_end_ms: z.number().nullable().optional(),
  min_duration_ms: z.number().nullable().optional(),
  max_duration_ms: z.number().nullable().optional(),
  custom_request: z.string().default(""),
  min_quality: z.number().nullable().optional(),
  diversity: z.enum(["balanced", "chronological", "quality"]).default("balanced"),
  revisit_underexplored: z.boolean().default(true),
  exclude_existing: z.boolean().default(true),
});
export type DiscoveryCriteria = z.infer<typeof discoveryCriteriaSchema>;

export const discoveryBucketSchema = z
  .object({
    index: z.number(),
    start_ms: z.number(),
    end_ms: z.number(),
    candidates: z.number(),
    new_candidates: z.number().default(0),
    covered_ms: z.number(),
    ratio: z.number(),
    status: z.enum(["unexplored", "thin", "covered"]),
  })
  .passthrough();
export type DiscoveryBucket = z.infer<typeof discoveryBucketSchema>;

export const discoveryCoverageSchema = z
  .object({
    duration_ms: z.number().default(0),
    covered_ms: z.number().default(0),
    ratio: z.number().default(0),
    buckets: z.array(discoveryBucketSchema).default([]),
    unexplored: z.number().default(0),
    thin: z.number().default(0),
    covered: z.number().default(0),
  })
  .passthrough();
export type DiscoveryCoverage = z.infer<typeof discoveryCoverageSchema>;

export const discoveryBatchSchema = z
  .object({
    id: z.string(),
    kind: z.enum(["initial", "additional"]).default("initial"),
    status: z.string(),
    created_at: z.string(),
    candidate_count: z.number(),
    requested: z.number().nullable().optional(),
    found: z.number().nullable().optional(),
    reason: z.string().nullable().optional(),
    exhausted: z.boolean().nullable().optional(),
    criteria: discoveryCriteriaSchema.partial().default({}),
    criteria_key: z.string().nullable().optional(),
    coverage: discoveryCoverageSchema.default({}),
    warnings: z.array(z.string()).default([]),
    converted: z.number().default(0),
  })
  .passthrough();
export type DiscoveryBatch = z.infer<typeof discoveryBatchSchema>;

export const discoveryStateSchema = z.object({
  batches: z.array(discoveryBatchSchema).default([]),
  counts: z.object({
    total_discovered: z.number(),
    accepted: z.number(),
    converted: z.number(),
    batches: z.number(),
  }),
  coverage: discoveryCoverageSchema.default({}),
  active_job: jobSchema.nullable().optional(),
  generating: z.boolean().default(false),
  last_result: discoveryBatchSchema.nullable().optional(),
});
export type DiscoveryState = z.infer<typeof discoveryStateSchema>;

export const discoveryRequestSchema = discoveryCriteriaSchema.partial({ count: true });
export type DiscoveryRequest = z.infer<typeof discoveryRequestSchema>;

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

export const productionRunSchema = z
  .object({
    id: z.string(),
    project_id: z.string(),
    status: z.enum([
      "queued",
      "running",
      "retrying",
      "awaiting_review",
      "partially_completed",
      "completed",
      "cancelled",
      "failed",
    ]),
    current_stage: z.string().nullable().optional(),
    progress: z.number(),
    message: z.string().default(""),
    request: z.record(z.string(), z.unknown()).default({}),
    provider: z.record(z.string(), z.unknown()).default({}),
    error_code: z.string().nullable().optional(),
    error_message: z.string().nullable().optional(),
    cancel_requested: z.boolean().default(false),
    job_id: z.string().nullable().optional(),
    created_at: z.string(),
    updated_at: z.string().optional(),
    started_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
  })
  .passthrough();
export type ProductionRun = z.infer<typeof productionRunSchema>;

export const productionStageSchema = z
  .object({
    name: z.string(),
    label: z.string(),
    status: z.enum(["pending", "running", "completed", "failed", "skipped"]),
    progress: z.number().default(0),
    message: z.string().default(""),
    attempt: z.number().default(0),
    error: z.string().nullable().optional(),
    started_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
  })
  .passthrough();
export type ProductionStage = z.infer<typeof productionStageSchema>;

export const productionFindingSchema = z
  .object({
    id: z.string(),
    severity: z.enum(["blocking", "warning", "info"]),
    category: z.enum(["editorial", "visual", "audio", "technical"]),
    title: z.string(),
    detail: z.string().default(""),
    output_range: z
      .object({ start_ms: z.number(), end_ms: z.number() })
      .nullable()
      .optional(),
    source_range: z
      .object({ start_ms: z.number(), end_ms: z.number() })
      .nullable()
      .optional(),
    evidence: z.array(z.string()).default([]),
    confidence: z.number().nullable().optional(),
    auto_repairable: z.boolean().default(false),
    proposed_repair: z.record(z.string(), z.unknown()).nullable().optional(),
    validation: z.string().default(""),
    origin: z.string().optional(),
  })
  .passthrough();
export type ProductionFinding = z.infer<typeof productionFindingSchema>;

export const productionReviewSchema = z
  .object({
    version: z.number().optional(),
    status: z.string().optional(),
    summary: z.string().default(""),
    findings: z.array(productionFindingSchema).default([]),
    checks: z.array(z.record(z.string(), z.unknown())).default([]),
    gate_status: z.string().nullable().optional(),
    gate_summary: z.string().nullable().optional(),
    model_review: z.record(z.string(), z.unknown()).default({}),
    approved_by_user: z.boolean().optional(),
    remaining_warnings: z.array(z.string()).default([]),
  })
  .passthrough();
export type ProductionReview = z.infer<typeof productionReviewSchema>;

export const productionClipSchema = z
  .object({
    id: z.string(),
    candidate_id: z.string().nullable(),
    ordinal: z.number(),
    status: z.string(),
    strategy: z.string().default(""),
    edl_version: z.number().default(1),
    render_id: z.string().nullable(),
    repair_attempts: z.number().default(0),
    technical_errors: z.number().default(0),
    findings: z.array(productionFindingSchema).default([]),
    review: productionReviewSchema.default({ summary: "", findings: [], checks: [], model_review: {}, remaining_warnings: [] }),
    plan: z.record(z.string(), z.unknown()).default({}),
    history: z.array(z.record(z.string(), z.unknown())).default([]),
  })
  .passthrough();
export type ProductionClip = z.infer<typeof productionClipSchema>;

export const exportSaveSchema = z
  .object({
    filename: z.string(),
    directory: z.string(),
    checksum: z.string().nullable().optional(),
    size_bytes: z.number().nullable().optional(),
    saved_at: z.string().nullable().optional(),
    render_id: z.string().optional(),
  })
  .passthrough();
export type ExportSave = z.infer<typeof exportSaveSchema>;

export const exportAssetSchema = z
  .object({
    asset_id: z.string(),
    render_id: z.string(),
    project_id: z.string(),
    filename: z.string(),
    version: z.number(),
    status: z.string().nullable(),
    current: z.boolean(),
    superseded: z.boolean(),
    review_status: z.string().nullable().optional(),
    strategy: z.string().nullable().optional(),
    edl_version: z.number().nullable().optional(),
    size_bytes: z.number().nullable().optional(),
    checksum: z.string().nullable().optional(),
    duration_ms: z.number().nullable().optional(),
    width: z.number().nullable().optional(),
    height: z.number().nullable().optional(),
    created_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
    download_url: z.string().nullable().optional(),
    review: z.record(z.string(), z.unknown()).default({}),
    saved: z.array(exportSaveSchema).default([]),
  })
  .passthrough();
export type ExportAsset = z.infer<typeof exportAssetSchema>;

export const pipelineStateSchema = z
  .object({
    run: productionRunSchema.nullable(),
    stages: z.array(productionStageSchema).default([]),
    clips: z.array(productionClipSchema).default([]),
    counts: z.record(z.string(), z.number()).default({}),
    job: jobSchema.nullable().optional(),
    manifest: z.record(z.string(), z.unknown()).nullable().optional(),
    capabilities: z.record(z.string(), z.unknown()).optional(),
  })
  .passthrough();
export type PipelineState = z.infer<typeof pipelineStateSchema>;

export const exportListSchema = z
  .object({
    project_id: z.string(),
    exports: z.array(exportAssetSchema).default([]),
    current: z.array(exportAssetSchema).default([]),
    superseded: z.array(exportAssetSchema).default([]),
  })
  .passthrough();
export type ExportList = z.infer<typeof exportListSchema>;

export const transcriptWordSchema = z.object({  start_ms: z.number(),
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
    kind: z.enum(["initial", "additional"]).default("initial"),
    superseded: z.boolean().default(false),
    discovery: z.record(z.string(), z.unknown()).default({}),
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
      speakers: z.array(z.string()).default([]),
      created_at: z.string(),
    })
    .nullable(),
  analysis: analysisRunSchema.nullable(),
  candidates: z.array(candidateSchema),
  renders: z.array(renderSchema),
  jobs: z.array(jobSchema),
  discovery: discoveryStateSchema.optional(),
});
export type ProjectDetail = z.infer<typeof projectDetailSchema>;

export const compressionPresetSchema = z.enum(["smallest", "balanced", "higher", "custom"]);
export type CompressionPreset = z.infer<typeof compressionPresetSchema>;

/** One resolved compression profile. Resolution is chosen separately. */
export const encodeSettingsSchema = z
  .object({
    preset: compressionPresetSchema,
    codec: z.enum(["h264", "hevc"]).default("h264"),
    crf: z.number().int().min(0).max(51),
    audio_bitrate_kbps: z.number().int().min(64).max(320),
    resolution_cap: z.number().int().min(320).max(3840),
    max_fps: z.number().nullable().optional(),
    x264_preset: z.string().optional(),
  })
  .passthrough();
export type EncodeSettings = z.infer<typeof encodeSettingsSchema>;

export const compressionChoiceSchema = z
  .object({
    preset: compressionPresetSchema,
  })
  .passthrough();
export type CompressionChoice = z.infer<typeof compressionChoiceSchema>;

export const enhancementProfileSchema = z.enum(["standard", "clean", "ai_fast", "ai", "ai_plus"]);
export type EnhancementProfile = z.infer<typeof enhancementProfileSchema>;

export const enhancementModelIdSchema = z.enum([
  "auto",
  "espcn-x2",
  "espcn-x4",
  "realesrgan-x2",
]);
export type EnhancementModelId = z.infer<typeof enhancementModelIdSchema>;

export const enhancementSettingsSchema = z.object({
  profile: enhancementProfileSchema.default("standard"),
  model_id: enhancementModelIdSchema.default("auto"),
  denoise: z.number().int().min(0).max(100).default(0),
  restoration: z.number().min(0).max(1).default(1),
  allow_fallback: z.boolean().default(true),
  temporal_stabilize: z.boolean().default(false),
  compare_first: z.boolean().default(false),
});
export type EnhancementSettings = z.infer<typeof enhancementSettingsSchema>;

export const enhancementBenchmarkSchema = z
  .object({
    model: z.string(),
    model_version: z.string().optional(),
    device: z.string(),
    seconds_per_input_megapixel: z.number(),
    measured_at: z.string().optional(),
    method: z.string().optional(),
  })
  .passthrough();

export const enhancementModelSchema = z
  .object({
    id: enhancementModelIdSchema.exclude(["auto"]),
    label: z.string(),
    engine: z.enum(["espcn", "realesrgan"]),
    scale: z.number(),
    version: z.string(),
    licence: z.string(),
    licence_url: z.string(),
    size_bytes: z.number(),
    installed: z.boolean(),
    bundled: z.boolean(),
    downloadable: z.boolean(),
    download_url: z.string().nullable(),
    sha256: z.string().nullable(),
    notes: z.string(),
    benchmark: enhancementBenchmarkSchema.nullable().optional(),
  })
  .passthrough();
export type EnhancementModel = z.infer<typeof enhancementModelSchema>;

export const enhancementCapabilitiesSchema = z
  .object({
    profiles: z.array(
      z.object({
        id: enhancementProfileSchema,
        label: z.string(),
        summary: z.string(),
        engine: z.string(),
        default_denoise: z.number(),
        default_restoration: z.number(),
        warnings: z.array(z.string()),
        uses_ai: z.boolean(),
      }),
    ),
    models: z.array(enhancementModelSchema),
    devices: z.object({
      providers: z.array(z.string()),
      device: z.string(),
      gpu_inference: z.boolean(),
      onnxruntime: z.boolean(),
      opencv_dnn_superres: z.boolean(),
      cpu_fallback: z.boolean(),
    }),
    gpu_required: z.boolean(),
    frame_interpolation: z.boolean(),
    model_download_requires_consent: z.boolean(),
    notes: z.array(z.string()),
  })
  .passthrough();
export type EnhancementCapabilities = z.infer<typeof enhancementCapabilitiesSchema>;

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
    limits: z.object({
      max_upload_bytes: z.number(),
      max_duration_ms: z.number(),
      max_moments_per_request: z.number().default(30),
    }),
    discovery: z
      .object({
        moment_types: z.array(discoveryMomentTypeSchema).default([]),
        max_count: z.number().default(30),
        reuses_transcript: z.boolean().default(true),
        supports_source_range: z.boolean().default(true),
        supports_speakers: z.boolean().default(true),
      })
      .optional(),
    download: z
      .object({
        qualities: z.array(
          z.object({ id: z.string(), label: z.string(), height: z.number().nullable() }),
        ),
        processing: z.array(
          z.object({
            id: z.string(),
            label: z.string(),
            default_quality: z.string().nullable(),
            proxy_height: z.number().nullable(),
            scene_fps: z.number().nullable(),
            warnings: z.array(z.string()).default([]),
          }),
        ),
        default_quality: z.string(),
        proxy_height_range: z.object({ min: z.number(), max: z.number() }),
        approximate_sizes: z.boolean().default(true),
      })
      .optional(),
    compression: z
      .object({
        presets: z.array(
          z.object({
            id: z.string(),
            label: z.string(),
            codec: z.string(),
            crf: z.number(),
            audio_bitrate_kbps: z.number(),
            resolution_cap: z.number(),
            max_fps: z.number().nullable(),
            note: z.string(),
          }),
        ),
        codecs: z.array(z.object({ id: z.string(), label: z.string() })),
        available_encoders: z.array(z.string()),
        gpu_required: z.boolean(),
      })
      .optional(),
    enhancement: enhancementCapabilitiesSchema.optional(),
    platform: z.object({ os: z.string(), release: z.string(), cpu_count: z.number(), python: z.string() }),
  })
  .passthrough();
export type Capabilities = z.infer<typeof capabilitiesSchema>;

/** Render request body accepted by POST /api/projects/{id}/renders. */
export const compositionModeSchema = z.enum([
  "auto",
  "crop",
  "fit",
  "pip",
  "split",
  "stack",
  "facecam_focus",
  "manual",
]);
export type CompositionMode = z.infer<typeof compositionModeSchema>;

export const renderRequestSchema = z.object({
  candidate_id: z.string().optional(),
  segments: z
    .array(z.object({ start_ms: z.number(), end_ms: z.number(), composition: z.record(z.string(), z.unknown()).optional() }))
    .optional(),
  aspect: z.enum(["9:16", "16:9", "1:1"]),
  resolution: z.union([z.literal(720), z.literal(1080), z.literal(1440), z.literal(2160)]),
  resolution_mode: z.enum(["class", "source", "custom"]).default("class"),
  custom_width: z.number().int().min(320).max(3840).optional(),
  custom_height: z.number().int().min(320).max(4320).optional(),
  enhancement: enhancementSettingsSchema.optional(),
  framing: z.enum(["crop", "blur"]),
  composition: compositionModeSchema.default("auto"),
  composition_overrides: z.record(z.string(), z.record(z.string(), z.unknown())).optional(),
  crop_x_pct: z.number().min(0).max(100).optional(),
  crop_y_pct: z.number().min(0).max(100).optional(),
  face_cam_position: z.enum(["top", "bottom"]).default("bottom"),
  face_cam_rect: z
    .object({
      x_pct: z.number().min(0).max(1),
      y_pct: z.number().min(0).max(1),
      width_pct: z.number().gt(0).max(1),
      height_pct: z.number().gt(0).max(1),
    })
    .optional(),
  face_cam_overrides: z
    .array(
      z.object({
        start_ms: z.number().min(0),
        end_ms: z.number().gt(0),
        rect: z.object({
          x_pct: z.number().min(0).max(1),
          y_pct: z.number().min(0).max(1),
          width_pct: z.number().gt(0).max(1),
          height_pct: z.number().gt(0).max(1),
        }),
        mode: z.enum(["lock", "track"]).default("lock"),
        reason: z.string().max(200).optional(),
      }),
    )
    .optional(),
  stack_split_pct: z.number().min(35).max(80).default(60),
  adaptive_stack: z.boolean().default(false),
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
  /** Independent of resolution: smallest / balanced / higher / custom. */
  compression: compressionChoiceSchema.optional(),
});
export type RenderRequest = z.infer<typeof renderRequestSchema>;

export const analysisRequestSchema = z.object({
  goal: z.string(),
  clip_count: z.number().min(1).max(30),
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
