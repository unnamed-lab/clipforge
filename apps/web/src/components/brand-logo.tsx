import Image from "next/image";

/** The fox-and-clapperboard mascot, always paired with the Clipforge wordmark. */
export function BrandLogo({ className }: { className?: string }) {
  return (
    <Image
      src="/brand/clipforge-mark.webp"
      alt=""
      width={56}
      height={56}
      className={className}
      priority
    />
  );
}
