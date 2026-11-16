import { readFile } from "node:fs/promises";
import path from "node:path";

/**
 * Server-side client for the Python worker.
 *
 * The browser never talks to the worker directly. Only server components and
 * route handlers import this module, and it is the only place the worker token
 * is read, so the token can never reach client JavaScript.
 */

const WORKER_URL = normaliseBaseUrl(process.env.CLIPFORGE_WORKER_URL ?? "http://127.0.0.1:8788");

/**
 * Render private services expose `host:port` without a scheme. Accept both forms
 * so the same variable works locally (`http://127.0.0.1:8788`) and on Render.
 */
function normaliseBaseUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) return "http://127.0.0.1:8788";
  return /^https?:\/\//i.test(trimmed) ? trimmed : `http://${trimmed}`;
}

export class WorkerError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "WorkerError";
    this.status = status;
    this.code = code;
  }
}

let cachedToken: string | null = null;

export async function workerToken(): Promise<string> {
  if (cachedToken) return cachedToken;

  const fromEnv = process.env.CLIPFORGE_WORKER_TOKEN?.trim();
  if (fromEnv) {
    cachedToken = fromEnv;
    return fromEnv;
  }

  const candidates = [
    path.resolve(process.cwd(), "data/worker-token"),
    path.resolve(process.cwd(), "../../data/worker-token"),
  ];
  for (const candidate of candidates) {
    try {
      const value = (await readFile(candidate, "utf8")).trim();
      if (value) {
        cachedToken = value;
        return value;
      }
    } catch {
      // try the next location
    }
  }

  throw new WorkerError(
    "The processing worker is not reachable. Start it locally with `pnpm worker:dev`, or check CLIPFORGE_WORKER_URL and the worker service.",
    503,
    "worker_unavailable",
  );
}

export function workerBaseUrl(): string {
  return WORKER_URL;
}

/** Build an outbound request to the worker with the shared token attached. */
export async function workerRequest(pathname: string, init: RequestInit = {}): Promise<Response> {
  const token = await workerToken();
  const url = `${WORKER_URL}${pathname.startsWith("/") ? pathname : `/${pathname}`}`;
  const headers = new Headers(init.headers);
  headers.set("x-clipforge-token", token);
  headers.delete("host");
  headers.delete("connection");

  try {
    return await fetch(url, { ...init, headers, cache: "no-store" });
  } catch {
    throw new WorkerError(
      `Could not reach the processing worker at ${WORKER_URL}. Start it with \`pnpm worker:dev\` or check the service URL.`,
      503,
      "worker_unreachable",
    );
  }
}

/** Read a JSON body from the worker, converting failures into WorkerError. */
export async function workerJson<T>(pathname: string, init: RequestInit = {}): Promise<T> {
  const response = await workerRequest(pathname, {
    ...init,
    headers: { "content-type": "application/json", ...(init.headers as Record<string, string> | undefined) },
  });

  if (!response.ok) {
    throw await toWorkerError(response);
  }
  return (await response.json()) as T;
}

export async function toWorkerError(response: Response): Promise<WorkerError> {
  let message = `The local worker returned HTTP ${response.status}.`;
  let code = "worker_error";
  try {
    const body = (await response.json()) as {
      error?: { message?: string; code?: string };
      detail?: string | Array<{ msg?: string }>;
    };
    if (body.error?.message) {
      message = body.error.message;
      code = body.error.code ?? code;
    } else if (typeof body.detail === "string") {
      message = body.detail;
    } else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      message = body.detail[0].msg;
      code = "validation_error";
    }
  } catch {
    // keep the generic message
  }
  return new WorkerError(message, response.status, code);
}

/** Normalise any thrown value into a message that is safe to render. */
export function errorMessage(error: unknown): string {
  if (error instanceof WorkerError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

export function errorCode(error: unknown): string {
  return error instanceof WorkerError ? error.code : "unknown_error";
}
