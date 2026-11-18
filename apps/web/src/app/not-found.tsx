import Link from "next/link";
import { ArrowLeft, Compass } from "lucide-react";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <div className="relative mb-6 grid size-16 place-items-center rounded-2xl border border-white/10 bg-ink-900 shadow-xl">
        <Compass className="size-8 text-ember-400 animate-pulse" />
      </div>
      <p className="label mb-2 text-ember-400">404 · Clip Not Found</p>
      <h1 className="text-3xl font-semibold tracking-tight text-mist-50">This cut doesn&apos;t exist</h1>
      <p className="mt-3 max-w-md text-sm text-mist-400">
        The recording, candidate, or studio view you&apos;re looking for was moved or never forged.
      </p>
      <Link href="/" className="btn btn-primary mt-6">
        <ArrowLeft className="size-4" />
        Return to Studio Desk
      </Link>
    </div>
  );
}
