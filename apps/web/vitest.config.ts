import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  // Next compiles JSX with the automatic runtime; the tests must match it.
  esbuild: { jsx: "automatic" },
  resolve: {
    // The shared package is consumed from source, exactly as Next does.
    alias: [
      { find: "@clipforge/shared", replacement: here("../../packages/shared/src/index.ts") },
      { find: /^@\//, replacement: `${here("./src")}/` },
    ],
  },
  test: {
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
});
