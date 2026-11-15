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
    created_at: z.string(),
    updated_at: z.string(),
  })
  .passthrough();
export type Candidate = z.infer<typeof candidateSchema>;

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
export const renderRequestSchema = z.object({
  candidate_id: z.string().optional(),
  segments: z.array(z.object({ start_ms: z.number(), end_ms: z.number() })).optional(),
  aspect: z.enum(["9:16", "16:9", "1:1"]),
  resolution: z.union([z.literal(720), z.literal(1080)]),
  framing: z.enum(["crop", "blur"]),
  caption_preset: z.enum(["clean", "large", "minimal", "branded", "custom"]),
  captions_enabled: z.boolean(),
  captions: z
    .object({
      font_family: z.string(),
      font_size: z.number(),
      bold: z.boolean(),
      position: z.enum(["bottom", "center", "top"]),
      margin_v: z.number(),
      primary_color: z.string(),
      highlight_color: z.string(),
      outline_color: z.string(),
      outline_width: z.number(),
      uppercase: z.boolean(),
      max_words_per_line: z.number(),
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
