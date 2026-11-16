import { workerPathFor } from "@/lib/proxy-path";
import { errorMessage, workerRequest, WorkerError } from "@/lib/worker";

/**
 * Catch-all proxy to the local worker.
 *
 * Every browser request to `/api/*` lands here. The worker token is injected
 * server-side, so it never appears in client bundles or devtools. Responses are
 * streamed straight through, which is what makes media range requests work.
 */

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "content-length",
  "range",
  "if-range",
  "if-none-match",
];

const DROPPED_RESPONSE_HEADERS = [
  "connection",
  "keep-alive",
  "transfer-encoding",
  "content-encoding",
  "content-length",
];

async function handle(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const url = new URL(request.url);
  let target: string;
  try {
    target = workerPathFor(path, url.search);
  } catch {
    return Response.json(
      { error: { code: "invalid_path", message: "That API path is not valid." } },
      { status: 400 },
    );
  }

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  try {
    const upstream = await workerRequest(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      // Node's fetch needs this when the body is a stream.
      ...(hasBody ? { duplex: "half" } : {}),
    } as RequestInit);

    const responseHeaders = new Headers();
    upstream.headers.forEach((value, key) => {
      if (!DROPPED_RESPONSE_HEADERS.includes(key.toLowerCase())) {
        responseHeaders.set(key, value);
      }
    });
    responseHeaders.set("x-clipforge-proxied", "worker");

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    const status = error instanceof WorkerError ? error.status : 502;
    const code = error instanceof WorkerError ? error.code : "proxy_error";
    return Response.json({ error: { code, message: errorMessage(error) } }, { status });
  }
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
export const HEAD = handle;
