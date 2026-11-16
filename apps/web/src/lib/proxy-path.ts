/**
 * Build the worker path for a browser request under `/api/[...path]`.
 *
 * The catch-all segment never includes the `/api` prefix, but every worker route
 * is mounted under `/api`, so the prefix has to be restored explicitly.
 */
export function workerPathFor(segments: readonly string[], search = ""): string {
  const parts = segments.filter((segment) => segment.length > 0);
  if (parts.some((part) => part === "." || part === ".." || part.includes("\\"))) {
    throw new Error("Invalid worker path");
  }
  const suffix = parts.length > 0 ? parts.join("/") : "";
  return `/api${suffix ? `/${suffix}` : ""}${search}`;
}
