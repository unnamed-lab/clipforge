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

export const DURATION_BANDS = [
  { id: "auto", label: "Mixed lengths", hint: "15–90s, chosen per moment" },
  { id: "short", label: "15–30s", hint: "Fast, single idea" },
  { id: "medium", label: "30–60s", hint: "Story beat or short argument" },
  { id: "long", label: "60–90s", hint: "Full explanation" },
] as const;

export const QUALITY_PROFILES = [
  { id: "fast", label: "Fast", hint: "Base model, quickest pass" },
  { id: "balanced", label: "Balanced", hint: "Default model, good accuracy" },
  { id: "best", label: "Best", hint: "Medium model, slow on CPU" },
] as const;

export const CAPTION_FONTS = ["Arial", "Segoe UI", "Inter", "Verdana", "Georgia", "Impact"] as const;
