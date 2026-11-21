# Branding

Clipforge is signed by a fox resting on a clapperboard, drawn in orange and violet on a near-black field. The product interface keeps its ember accent (`#e9b44c`) for controls and focus states; the illustration is the brand layer, not a replacement palette.

## Assets

| Asset | File | Size | Used by |
| --- | --- | --- | --- |
| Banner | `apps/web/public/brand/clipforge-banner.webp` | 1600×900 | README header |
| Mark | `apps/web/public/brand/clipforge-mark.webp` | 560×560 | App header, 404 page |
| Icon | `apps/web/public/brand/clipforge-icon.webp` | 512×512 | Square logo for external use |
| Favicon | `apps/web/src/app/icon.png` | 256×256 | Browser tab, served by the Next.js icon convention |
| Apple icon | `apps/web/src/app/apple-icon.png` | 180×180 | iOS home screen |
| Social card | `apps/web/src/app/opengraph-image.jpg` | 1200×630 | Link previews, served by the Next.js metadata convention |

The header uses [`BrandLogo`](../apps/web/src/components/brand-logo.tsx), which renders the mark as a clipped square next to the wordmark. Next.js generates the favicon and Open Graph tags from the files in `apps/web/src/app`, so there is no manual icon table to keep in sync.

## Palette

| Role | Value | Notes |
| --- | --- | --- |
| Interface accent | `#e9b44c` | `ember-500` in `globals.css`; unchanged by the illustration. |
| Brand ink | `#0b0614` | Background of the mark and banner. |
| Mascot orange | `#ff6a1a` | Fox fur and the clapperboard highlight. |
| Mascot violet | `#7a2bff` | "forge" in the wordmark and the feature icons. |
| Cream | `#fff4ea` | Fox face, captions and the clapperboard stripes. |

## Usage

- Keep the wordmark in the banner intact; do not crop it into a square.
- The mark is cropped for the header. Use the icon file for square placements instead of squeezing the banner.
- Keep clear space around the mark equal to roughly one ear width.
- On dark surfaces, the mark's own near-black background blends in; add a thin ring or a container rather than recolouring the artwork.
