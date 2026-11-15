import type { Metadata } from "next";
import type { ReactNode } from "react";
import { SiteHeader } from "@/components/site-header";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Clipforge — find the clips hidden in your recordings",
    template: "%s · Clipforge",
  },
  description:
    "A local-first video repurposing studio. Transcribe, find the moments worth keeping, edit the edges, and export captioned vertical clips without sending your media anywhere.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        <SiteHeader />
        <main className="mx-auto w-full max-w-[1240px] px-5 pb-24 pt-8">{children}</main>
        <footer className="border-t border-white/6">
          <div className="mx-auto flex w-full max-w-[1240px] flex-wrap items-center justify-between gap-2 px-5 py-5 text-[11.5px] text-mist-600">
            <p>Clipforge runs on this machine. Your media never leaves it unless you opt in.</p>
            <p className="numeric">Scores are estimates, not guarantees of reach.</p>
          </div>
        </footer>
      </body>
    </html>
  );
}
