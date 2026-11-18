const RADIUS = 8.5;
const HOLE_RADIUS = 0.8;

/**
 * Sprocket holes sit on the band's centre line, inside the 35°–325° arc that
 * leaves the "C" open on the right.
 */
const SPROCKET_HOLES = Array.from({ length: 8 }, (_, index) => {
  const degrees = 35 + 36.25 * (index + 0.5);
  const radians = (degrees * Math.PI) / 180;
  const cx = (12 + RADIUS * Math.cos(radians)).toFixed(2);
  const cy = (12 + RADIUS * Math.sin(radians)).toFixed(2);
  const r = HOLE_RADIUS;
  return `M${(Number(cx) - r).toFixed(2)} ${cy}a${r} ${r} 0 1 0 ${r * 2} 0a${r} ${r} 0 1 0 ${-r * 2} 0Z`;
}).join("");

const MARK_PATH =
  `M21.01 18.31A11 11 0 1 1 21.01 5.69L16.92 8.56A6 6 0 1 0 16.92 15.44Z` + SPROCKET_HOLES;

/** A film-strip "C": an arc of leader with sprocket holes punched through it. */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={className} fill="currentColor">
      <path d={MARK_PATH} fillRule="evenodd" />
    </svg>
  );
}

export const BRAND_MARK_PATH = MARK_PATH;
