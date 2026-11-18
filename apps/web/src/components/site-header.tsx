"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { FolderOpen, LayoutTemplate, Menu, Plus, Settings, X } from "lucide-react";
import { BrandMark } from "@/components/brand-mark";
import { Tooltip } from "@/components/ui";
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

function workerHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function SiteHeader({ workerUrl }: { workerUrl: string }) {
  const pathname = usePathname() ?? "/";
  const [worker, setWorker] = useState<"checking" | "online" | "offline">("checking");
  const [menuOpen, setMenuOpen] = useState(false);
  const host = workerHost(workerUrl);

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

  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [menuOpen]);

  const status =
    worker === "online"
      ? { label: "Worker online", dot: "bg-signal-good", hint: `Local worker responding on ${host}` }
      : worker === "offline"
        ? { label: "Worker offline", dot: "bg-signal-bad", hint: `No response from ${host}` }
        : { label: "Checking", dot: "bg-mist-600 animate-pulse", hint: `Looking for the worker at ${host}` };

  return (
    <header className="sticky top-0 z-40 border-b border-white/8 bg-ink-950/85 backdrop-blur-lg">
      <div className="relative mx-auto flex h-14 w-full max-w-[1240px] items-center gap-3 px-4 sm:gap-6 sm:px-5">
        <Link
          href="/"
          className="group -my-2 flex shrink-0 items-center gap-2.5 py-2"
          aria-label="Clipforge home"
        >
          <BrandMark className="size-7 text-ember-500" />
          <span className="text-[15px] font-semibold tracking-[0.015em] text-mist-50">Clipforge</span>
        </Link>

        <nav aria-label="Main" className="hidden items-center gap-1 sm:flex">
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
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex items-center gap-2 sm:gap-3">
          <div className="hidden sm:block">
            <Tooltip label={status.hint}>
              <span className="flex items-center gap-1.5 text-[11px] text-mist-500" aria-label={status.hint}>
                <span className={cn("size-1.5 rounded-full", status.dot)} />
                {status.label}
              </span>
            </Tooltip>
          </div>

          <Link href="/new" className="btn btn-primary btn-sm w-10 px-0 sm:w-auto sm:px-3" aria-label="New project">
            <Plus className="size-3.5" strokeWidth={2.4} />
            <span className="hidden sm:inline">New project</span>
          </Link>

          <button
            type="button"
            className="btn btn-ghost btn-sm w-10 px-0 sm:hidden"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="mobile-nav"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
          >
            {menuOpen ? <X className="size-4" strokeWidth={2} /> : <Menu className="size-4" strokeWidth={2} />}
          </button>
        </div>

        {menuOpen ? (
          <div
            id="mobile-nav"
            className="absolute inset-x-0 top-full border-b border-white/8 bg-ink-950 shadow-[0_28px_56px_-24px_rgba(0,0,0,0.95)] sm:hidden"
          >
            <nav aria-label="Main" className="space-y-1 px-4 py-3">
              {NAV.map((item) => {
                const active = isActive(pathname, item.href);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    onClick={() => setMenuOpen(false)}
                    className={cn(
                      "flex h-11 items-center gap-3 rounded-lg px-3 text-[14px] transition-colors",
                      active
                        ? "bg-white/[0.07] text-mist-50"
                        : "text-mist-300 hover:bg-white/[0.04] hover:text-mist-100",
                    )}
                  >
                    <Icon className="size-4" strokeWidth={1.9} />
                    {item.label}
                  </Link>
                );
              })}
            </nav>
            <div className="flex items-center gap-1.5 border-t border-white/6 px-4 py-3 text-[11.5px] text-mist-500">
              <span className={cn("size-1.5 rounded-full", status.dot)} />
              {status.hint}
            </div>
          </div>
        ) : null}
      </div>
    </header>
  );
}
