import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { DM_Sans } from "next/font/google";
import { SiteHeader } from "@/components/site-header";
import { workerBaseUrl } from "@/lib/worker";
import "./globals.css";

const dmSans = DM_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-dm-sans",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Clipforge — find the clips hidden in your recordings",
    template: "%s · Clipforge",
  },
  description:
    "A local-first video repurposing studio. Transcribe, find the moments worth keeping, edit the edges, and export captioned vertical clips without sending your media anywhere.",
  applicationName: "Clipforge",
  icons: {
    icon: [{ url: "/favicon.svg", type: "image/svg+xml" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0b0d",
  colorScheme: "dark",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={dmSans.variable}>
      <body className="flex min-h-screen flex-col antialiased">
        <SiteHeader workerUrl={workerBaseUrl()} />
        <main className="mx-auto w-full max-w-[1240px] flex-1 px-4 pb-16 pt-6 sm:px-5 sm:pb-24 sm:pt-8">{children}</main>
        <footer className="border-t border-white/6">
          <div className="mx-auto flex w-full max-w-[1240px] flex-col gap-2 px-4 py-5 text-[11.5px] text-mist-600 sm:flex-row sm:items-center sm:justify-between sm:px-5">
            <p className="max-w-[62ch] leading-relaxed">
              Everything runs on this machine — your media stays here, and a score is an estimate of
              interest, not a promise of reach.
            </p>
            <p className="font-mono text-[11px] text-mist-700">whisper · ffmpeg · sqlite</p>
          </div>
        </footer>
      </body>
    </html>
  );
}
