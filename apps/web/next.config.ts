import path from "node:path";
import type { NextConfig } from "next";

const standalone = process.env.NEXT_OUTPUT === "standalone";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Standalone tracing is needed for the container image. It is opt-in because
  // Next copies pnpm symlinks during tracing, which fails on Windows without
  // Developer Mode. The web Dockerfile sets NEXT_OUTPUT=standalone.
  ...(standalone
    ? {
        output: "standalone" as const,
        // Start tracing at the monorepo root so the workspace package
        // (@clipforge/shared) and pnpm links are included in the runtime image.
        outputFileTracingRoot: path.join(__dirname, "../.."),
      }
    : {}),
  transpilePackages: ["@clipforge/shared"],
  experimental: {
    serverActions: {
      bodySizeLimit: "2gb",
    },
  },
  eslint: {
    ignoreDuringBuilds: false,
  },
};

export default nextConfig;
