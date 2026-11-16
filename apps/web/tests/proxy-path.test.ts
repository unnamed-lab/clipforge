import { describe, expect, it } from "vitest";

import { workerPathFor } from "../src/lib/proxy-path";

describe("workerPathFor", () => {
  it("restores the /api prefix for a single segment", () => {
    expect(workerPathFor(["health"])).toBe("/api/health");
  });

  it("keeps nested endpoints and the query string intact", () => {
    expect(workerPathFor(["projects", "abc", "renders"], "?limit=2")).toBe(
      "/api/projects/abc/renders?limit=2",
    );
  });

  it("handles the bare /api root", () => {
    expect(workerPathFor([])).toBe("/api");
  });

  it("rejects traversal segments instead of forwarding them", () => {
    expect(() => workerPathFor(["..", "secrets"])).toThrow("Invalid worker path");
    expect(() => workerPathFor(["projects\\..\\secrets"])).toThrow("Invalid worker path");
  });
});
