"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { RefreshCw } from "lucide-react";
import { Spinner } from "@/components/ui";

export function RefreshDiagnostics() {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [clicked, setClicked] = useState(false);

  return (
    <button
      type="button"
      className="btn btn-sm"
      disabled={pending || clicked}
      onClick={() => {
        setClicked(true);
        startTransition(() => {
          router.refresh();
          window.setTimeout(() => setClicked(false), 800);
        });
      }}
    >
      {pending || clicked ? <Spinner /> : <RefreshCw className="size-3.5" strokeWidth={2} />}
      Refresh
    </button>
  );
}
