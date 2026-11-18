import Link from "next/link";
import type { Metadata } from "next";
import { CAPTION_PRESETS } from "@clipforge/shared";
import { SectionHeading } from "@/components/ui";

export const metadata: Metadata = { title: "Templates" };

const RECIPES = [
  {
    id: "talking-head",
    name: "Talking head vertical",
    aspect: "9:16",
    framing: "Crop",
    resolution: "1080p",
    captions: "large",
    use: "Interviews, podcasts and solo commentary where the speaker fills the middle of the frame.",
  },
  {
    id: "quiet-subtitles",
    name: "Quiet subtitles",
    aspect: "16:9",
    framing: "Crop",
    resolution: "1080p",
    captions: "minimal",
    use: "Explainers and screen recordings where the visuals matter more than the text.",
  },
  {
    id: "square-quote",
    name: "Square quote card",
    aspect: "1:1",
    framing: "Blurred background",
    resolution: "1080p",
    captions: "branded",
    use: "Feed posts built from a single strong line. Blur keeps a wide source intact.",
  },
  {
    id: "podcast-bottom",
    name: "Podcast bottom bar",
    aspect: "9:16",
    framing: "Blurred background",
    resolution: "720p",
    captions: "clean",
    use: "Two-person audio-led conversations, and the fastest export on a laptop.",
  },
] as const;

export default function TemplatesPage() {
  return (
    <div className="space-y-7">
      <div className="space-y-2">
        <p className="label">Templates</p>
        <h1 className="text-[24px] font-semibold leading-tight text-mist-50">Recipes</h1>
        <p className="max-w-2xl text-[13.5px] leading-relaxed text-mist-400">
          Four starting points for the export panel. Each one sets framing, resolution and captions — none
          of them changes how moments are ranked.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {RECIPES.map((recipe) => {
          const preset = CAPTION_PRESETS.find((entry) => entry.id === recipe.captions) ?? CAPTION_PRESETS[0];
          return (
            <article key={recipe.id} className="surface flex flex-col gap-4 p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <h2 className="text-[14px] font-medium text-mist-50">{recipe.name}</h2>
                  <p className="text-[12px] leading-relaxed text-mist-500">{recipe.use}</p>
                </div>
                <div className="flex h-[100px] shrink-0 items-start justify-end">
                  <CaptionSample
                    aspect={recipe.aspect}
                    color={preset.sample.highlight}
                    weight={preset.sample.weight}
                    size={preset.sample.size}
                    position={preset.sample.position}
                  />
                </div>
              </div>

              <dl className="grid grid-cols-1 gap-y-1.5 text-[11.5px] sm:grid-cols-2 sm:gap-x-4">
                {[
                  ["Frame", recipe.aspect],
                  ["Fill", recipe.framing],
                  ["Resolution", recipe.resolution],
                  ["Captions", preset.label],
                ].map(([term, value]) => (
                  <div key={term} className="flex items-baseline justify-between gap-2 border-b border-white/5 pb-1">
                    <dt className="text-mist-600">{term}</dt>
                    <dd className="text-right text-mist-300">{value}</dd>
                  </div>
                ))}
              </dl>

              <Link href="/new" className="btn btn-sm mt-auto self-start">
                Import a video to use it
              </Link>
            </article>
          );
        })}
      </div>

      <SectionHeading
        eyebrow="Caption styles"
        title="What each preset looks like"
        description="These samples use the same colours and weights the renderer burns into the MP4. The highlighted word is the word currently being spoken."
      />

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {CAPTION_PRESETS.map((preset) => (
          <div key={preset.id} className="surface space-y-3 p-4">
            <div className="relative grid aspect-[4/3] place-items-center overflow-hidden rounded-lg border border-white/8 bg-gradient-to-br from-ink-800 to-ink-950 px-3">
              <span
                className="text-center leading-tight"
                style={{
                  color: "#ffffff",
                  fontWeight: preset.sample.weight,
                  fontSize: `${Math.min(20, preset.sample.size * 0.72)}px`,
                  textShadow: "0 1px 3px rgba(0,0,0,0.9)",
                  alignSelf: preset.sample.position === "center" ? "center" : "end",
                }}
              >
                keep the sentence
                <br />
                <span style={{ color: preset.sample.highlight }}>that stands alone</span>
              </span>
            </div>
            <div>
              <p className="text-[13px] font-medium text-mist-100">{preset.label}</p>
              <p className="mt-0.5 text-[11.5px] leading-relaxed text-mist-500">{preset.description}</p>
            </div>
          </div>
        ))}
      </div>

      <p className="text-[11.5px] leading-relaxed text-mist-600">
        Caption styling is generated with libass, which is why the samples can only approximate the final
        result: font metrics depend on the font file FFmpeg selects on this machine.
      </p>
    </div>
  );
}

function CaptionSample({
  aspect,
  color,
  weight,
  size,
  position,
}: {
  aspect: string;
  color: string;
  weight: number;
  size: number;
  position: string;
}) {
  const ratio = aspect === "9:16" ? "aspect-[9/16]" : aspect === "1:1" ? "aspect-square" : "aspect-video";
  const vertical =
    position === "center"
      ? { top: "50%", transform: "translateY(-50%)" }
      : position === "top"
        ? { top: "5px" }
        : { bottom: "5px" };
  return (
    <div className={`relative w-14 shrink-0 overflow-hidden rounded border border-white/10 bg-ink-850 ${ratio}`}>
      <div
        aria-hidden
        className="absolute inset-0 opacity-60"
        style={{
          backgroundImage:
            "radial-gradient(circle at 50% 38%, rgba(255,255,255,0.22), transparent 42%), linear-gradient(180deg, rgba(255,255,255,0.06), rgba(0,0,0,0.5))",
        }}
      />
      <span
        aria-hidden
        className="absolute inset-x-1 text-center leading-none"
        style={{
          ...vertical,
          color,
          fontWeight: weight,
          fontSize: `${Math.max(5, size * 0.34)}px`,
        }}
      >
        ▌▌▌
      </span>
    </div>
  );
}
