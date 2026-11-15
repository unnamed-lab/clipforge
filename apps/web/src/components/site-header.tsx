"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { FolderOpen, LayoutTemplate, Plus, Settings } from "lucide-react";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Projects", icon: FolderOpen },
  { href: "/new", label: "Create", icon: Plus },
  { href: "/templates", label: "Templates", icon: LayoutTemplate },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

function isActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  if (href === "/new") return pathname === "/new" || pathname.startsWith("/projects/");
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SiteHeader() {
  const pathname = usePathname() ?? "/";
  const [worker, setWorker] = useState<"checking" | "online" | "offline">("checking");

  useEffect(() => {
    let cancelled = false;
    fetch("/api/health", { cache: "no-store" })
      .then((response) => {
        if (!cancelled) setWorker(response.ok ? "online" : "offline");
      })
      .catch(() => {
        if (!cancelled) setWorker("offline");
      });
    return () => {
      cancelled = true;
    };
  }, [pathname]);

  return (
    <header className="sticky top-0 z-40 border-b border-white/8 bg-ink-950/80 backdrop-blur-xl">
      <div className="mx-auto flex h-14 w-full max-w-[1240px] items-center gap-6 px-5">
        <Link href="/" className="group flex items-center gap-2.5">
          <span className="relative grid size-7 place-items-center rounded-[7px] bg-ember-500 text-[13px] font-black text-ink-950">
            C
            <span className="absolute inset-0 rounded-[7px] ring-1 ring-inset ring-white/25" />
          </span>
          <span className="text-[15px] font-semibold tracking-tight text-mist-50">Clipforge</span>
        </Link>

        <nav aria-label="Main" className="flex items-center gap-1">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex h-8 items-center gap-1.5 rounded-md px-2.5 text-[13px] transition-colors",
                  active
                    ? "bg-white/[0.07] text-mist-50"
                    : "text-mist-400 hover:bg-white/[0.04] hover:text-mist-100",
                )}
              >
                <Icon className="size-3.5" strokeWidth={1.9} />
                <span className="hidden sm:inline">{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-3">
          <span
            className="hidden items-center gap-1.5 text-[11px] text-mist-500 sm:flex"
            title={
              worker === "online"
                ? "The local worker is responding on 127.0.0.1:8788"
                : worker === "offline"
                  ? "The local worker is not responding"
                  : "Checking the local worker"
            }
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                worker === "online"
                  ? "bg-signal-good"
                  : worker === "offline"
                    ? "bg-signal-bad"
                    : "animate-pulse bg-mist-600",
              )}
            />
            {worker === "online" ? "Worker online" : worker === "offline" ? "Worker offline" : "Checking"}
          </span>
          <Link href="/new" className="btn btn-primary btn-sm">
            <Plus className="size-3.5" strokeWidth={2.4} />
            New project
          </Link>
        </div>
      </div>
    </header>
  );
}
