import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { BrandMark } from "@/components/brand-mark";

export default function NotFound() {
  return (
    <div className="flex min-h-[62vh] flex-col items-center justify-center px-1 text-center">
      <div className="mb-6 grid size-16 place-items-center rounded-xl border border-white/10 bg-ink-900">
        <BrandMark className="size-9 text-ember-500" />
      </div>
      <p className="label mb-2">404</p>
      <h1 className="text-[26px] font-semibold leading-tight text-mist-50 sm:text-3xl">
        This cut doesn&apos;t exist
      </h1>
      <p className="mt-3 max-w-sm text-[13.5px] leading-relaxed text-mist-400">
        The page you followed was moved, or it never made it out of the cutting room.
      </p>
      <div className="mt-6 flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
        <Link href="/" className="btn btn-primary w-full sm:w-auto">
          <ArrowLeft className="size-4" strokeWidth={2} />
          Back to projects
        </Link>
        <Link href="/new" className="btn w-full sm:w-auto">
          Start a project
        </Link>
      </div>
    </div>
  );
}
