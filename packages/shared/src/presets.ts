export const ASPECTS = [
  { id: "9:16", label: "Vertical 9:16", hint: "Reels, Shorts, TikTok" },
  { id: "16:9", label: "Landscape 16:9", hint: "YouTube, web embeds" },
  { id: "1:1", label: "Square 1:1", hint: "Feed posts" },
] as const;

export type AspectId = (typeof ASPECTS)[number]["id"];

export const CAPTION_PRESETS = [
  {
    id: "clean",
    label: "Clean podcast",
    description: "Bottom-anchored captions with a single highlighted word.",
    sample: { size: 22, weight: 700, position: "bottom", highlight: "#E9B44C" },
  },
  {
    id: "large",
    label: "Large highlighted",
    description: "Centred, uppercase, three words at a time. Built for vertical.",
    sample: { size: 27, weight: 800, position: "center", highlight: "#5EEAD4" },
  },
  {
    id: "minimal",
    label: "Minimal subtitles",
    description: "Small, quiet, seven words per line. Nothing competes with the frame.",
    sample: { size: 18, weight: 400, position: "bottom", highlight: "#F5F5F5" },
  },
  {
    id: "branded",
    label: "Branded bar",
    description: "Boxed text with a lime accent for a consistent channel look.",
    sample: { size: 21, weight: 700, position: "bottom", highlight: "#A3E635" },
  },
] as const;

export type CaptionPresetId = (typeof CAPTION_PRESETS)[number]["id"];

export const DEFAULT_CAPTION_PRESET: CaptionPresetId = "clean";

export const CAPTION_POSITIONS = [
  { id: "bottom", label: "Bottom", hint: "Default, inside the platform-safe area" },
  { id: "center", label: "Middle", hint: "Centred, for sparse frames" },
  { id: "top", label: "Top", hint: "Above faces and lower content" },
] as const;

export type CaptionPositionId = (typeof CAPTION_POSITIONS)[number]["id"];
export const DEFAULT_CAPTION_POSITION: CaptionPositionId = "bottom";

export const CAPTION_ALIGNMENTS = [
  { id: "left", label: "Left" },
  { id: "center", label: "Centre" },
  { id: "right", label: "Right" },
] as const;

export type CaptionAlignmentId = (typeof CAPTION_ALIGNMENTS)[number]["id"];
export const DEFAULT_CAPTION_ALIGNMENT: CaptionAlignmentId = "center";
export const DEFAULT_CAPTION_MARGIN_V = 150;

export const GOALS = [
  {
    id: "best_moments",
    label: "Best moments",
    description: "Balanced picks across the whole recording.",
  },
  {
    id: "social_clips",
    label: "Short social clips",
    description: "Strong opening seconds, quotable, self-contained.",
  },
  {
    id: "educational",
    label: "Educational highlights",
    description: "Dense with explanation and complete reasoning.",
  },
  {
    id: "funny",
    label: "Funny and memorable",
    description: "Laughter, surprise and lines that land out of context.",
  },
  {
    id: "debate",
    label: "Strong opinions",
    description: "Disagreement and claims worth arguing with.",
  },
  {
    id: "emotional_stories",
    label: "Emotional stories",
    description: "Personal anecdotes with a beginning and an end.",
  },
  {
    id: "tutorials",
    label: "Tutorials and takeaways",
    description: "Practical steps you can follow without the rest.",
  },
] as const;

export type GoalId = (typeof GOALS)[number]["id"];

/**
 * Editorial duration targets. These are not platform limits: a 60-90s edit can
 * still be uploaded anywhere that accepts three minutes. Clipforge never pads or
 * truncates a moment to hit a preset.
 */
export const DURATION_PRESETS = [
  {
    id: "short",
    label: "Short",
    hint: "15-30s · one idea, one payoff",
    min_ms: 15_000,
    max_ms: 30_000,
    structure: "Hook → payoff",
  },
  {
    id: "standard",
    label: "Standard",
    hint: "30-60s · hook, context, main, payoff",
    min_ms: 30_000,
    max_ms: 60_000,
    structure: "Hook → context → main → payoff",
  },
  {
    id: "extended",
    label: "Extended (default)",
    hint: "60-90s · room for development",
    min_ms: 60_000,
    max_ms: 90_000,
    structure: "Hook → context → main → development → payoff",
  },
  {
    id: "custom",
    label: "Custom",
    hint: "Choose a validated minimum and maximum",
    min_ms: 5_000,
    max_ms: 180_000,
    structure: "Your range, validated",
  },
] as const;

export type DurationPresetId = (typeof DURATION_PRESETS)[number]["id"];

/** Legacy request band kept for old clients; new UI sends a duration preset. */
export const DURATION_BANDS = [
  { id: "auto", label: "Mixed lengths", hint: "Uses the brief's duration preset" },
  { id: "short", label: "15-30s", hint: "Fast, single idea" },
  { id: "medium", label: "30-60s", hint: "Story beat or short argument" },
  { id: "long", label: "60-90s", hint: "Full explanation" },
] as const;

export const EDITORIAL_STYLES = [
  { id: "automatic", label: "Automatic", hint: "Choose the best supported strategy" },
  { id: "hook_first", label: "Hook first", hint: "Prefer a cold open when the source supports it" },
  { id: "chronological", label: "Chronological", hint: "Never move source order" },
  { id: "manual", label: "Manual", hint: "You order the cut yourself" },
] as const;

export const EDITORIAL_STRATEGIES = [
  { id: "automatic", label: "Let Clipforge choose", reorders: false },
  { id: "cold_open", label: "Cold open → flashback → payoff", reorders: true },
  { id: "in_medias_res", label: "In medias res", reorders: true },
  { id: "result_first", label: "Result first", reorders: true },
  { id: "chronological", label: "Chronological", reorders: false },
  { id: "question_answer", label: "Question → answer", reorders: false },
  { id: "story_escalation", label: "Escalation → climax", reorders: false },
  { id: "reaction_led", label: "Reaction led", reorders: false },
  { id: "manual", label: "Manual ordering", reorders: false },
] as const;
export type EditorialStrategyId = (typeof EDITORIAL_STRATEGIES)[number]["id"];

export const PACING_PROFILES = [
  { id: "calm", label: "Calm", hint: "Keeps breaths and long pauses" },
  { id: "conversational", label: "Conversational", hint: "Light dead-air removal" },
  { id: "balanced", label: "Balanced", hint: "Default; removes only long pause middles" },
  { id: "energetic", label: "Energetic", hint: "Tighter pauses, still sentence-safe" },
  { id: "fast", label: "Fast", hint: "Fastest safe pacing; review the cuts" },
] as const;

export const HOOK_PREFERENCES = [
  { id: "auto", label: "Automatic" },
  { id: "question", label: "Question" },
  { id: "outcome", label: "Outcome / result" },
  { id: "curiosity", label: "Curiosity gap" },
  { id: "story", label: "Story opening" },
  { id: "humour", label: "Humour" },
  { id: "emotional", label: "Emotional" },
  { id: "contrarian", label: "Contrarian" },
  { id: "chronological", label: "Chronological opening" },
  { id: "manual", label: "Choose the hook myself" },
] as const;

export const PATTERN_INTERRUPTS = [
  { id: "off", label: "Off" },
  { id: "subtle", label: "Subtle" },
  { id: "balanced", label: "Balanced" },
  { id: "strong", label: "Strong" },
] as const;

export const REACTION_SHOTS = [
  { id: "automatic", label: "Automatic" },
  { id: "prefer", label: "Prefer reactions" },
  { id: "avoid", label: "Avoid reactions" },
  { id: "manual", label: "Only when I choose" },
] as const;

export const LOOP_ENDINGS = [
  { id: "automatic", label: "Automatic" },
  { id: "enabled", label: "Loop when it is honest" },
  { id: "disabled", label: "Never loop" },
  { id: "manual", label: "Only when I choose" },
] as const;

export const EDITORIAL_GENRES = [
  { id: "podcast_interview", label: "Podcast / interview" },
  { id: "comedy", label: "Comedy" },
  { id: "storytime", label: "Storytime" },
  { id: "education", label: "Education" },
  { id: "expert_advice", label: "Expert advice" },
  { id: "debate", label: "Debate" },
  { id: "emotional_inspirational", label: "Emotional / inspirational" },
  { id: "product_demo", label: "Product demo" },
  { id: "livestream_highlights", label: "Livestream highlights" },
  { id: "slide_webinar", label: "Slide-based / webinar" },
] as const;

export const QUALITY_PROFILES = [
  { id: "fast", label: "Fast", hint: "Base model, quickest pass" },
  { id: "balanced", label: "Balanced", hint: "Default model, good accuracy" },
  { id: "best", label: "Best", hint: "Medium model, slow on CPU" },
] as const;

export const COMPOSITION_MODES = [
  { id: "auto", label: "Auto", hint: "Detect slides, screens and speakers" },
  { id: "crop", label: "Fill / crop", hint: "Fill the frame; edges may be lost" },
  { id: "fit", label: "Contain", hint: "Show the whole source over a blurred fill" },
  { id: "pip", label: "Picture in picture", hint: "Full source over a blurred background" },
  { id: "split", label: "Split / stacked", hint: "Unavailable for a single source" },
  { id: "manual", label: "Manual", hint: "Per-scene overrides in the editor" },
] as const;

export type CompositionModeId = (typeof COMPOSITION_MODES)[number]["id"];

/** Bundled, redistributable caption faces. Requested proprietary faces map to these. */
export const CAPTION_FONTS = [
  { id: "Montserrat", label: "Montserrat", note: "Bold geometric; the default" },
  { id: "Inter", label: "Inter", note: "Requested for SF Pro Display" },
  { id: "Archivo Black", label: "Archivo Black", note: "Requested for The Bold Font" },
  { id: "Bebas Neue", label: "Bebas Neue", note: "Condensed uppercase" },
  { id: "Lato", label: "Lato", note: "Requested for Helvetica / Arial" },
  { id: "Anton", label: "Anton", note: "Requested for Impact" },
] as const;
