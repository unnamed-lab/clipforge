/** Browser-side helpers for talking to the Next.js proxy, never the worker directly. */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

type JsonInit = Omit<RequestInit, "body"> & { body?: unknown };

export async function apiJson<T>(pathname: string, init: JsonInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const response = await fetch(pathname, {
    ...rest,
    headers: {
      accept: "application/json",
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}.`;
    let code = "request_failed";
    try {
      const parsed = (await response.json()) as {
        error?: { message?: string; code?: string };
        detail?: string;
      };
      if (parsed.error?.message) {
        message = parsed.error.message;
        code = parsed.error.code ?? code;
      } else if (typeof parsed.detail === "string") {
        message = parsed.detail;
      }
    } catch {
      // response was not JSON
    }
    throw new ApiError(message, response.status, code);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export type UploadHandle = {
  promise: Promise<{ source: unknown }>;
  abort: () => void;
};

/** Upload through XHR so the browser can report real bytes sent. */
export function uploadSource(
  projectId: string,
  file: File,
  onProgress?: (fraction: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<{ source: unknown }>((resolve, reject) => {
    xhr.open("POST", `/api/projects/${encodeURIComponent(projectId)}/sources/upload`);
    xhr.responseType = "json";

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable || !onProgress) return;
      onProgress(Math.min(1, event.loaded / event.total));
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve((xhr.response ?? {}) as { source: unknown });
        return;
      }
      const payload = xhr.response as
        | { error?: { message?: string; code?: string }; detail?: string }
        | null;
      reject(
        new ApiError(
          payload?.error?.message ?? payload?.detail ?? `Upload failed with HTTP ${xhr.status}.`,
          xhr.status,
          payload?.error?.code ?? "upload_failed",
        ),
      );
    });

    xhr.addEventListener("error", () => {
      reject(new ApiError("The upload could not reach the local worker.", 0, "network_error"));
    });

    xhr.addEventListener("abort", () => {
      reject(new ApiError("Upload cancelled.", 0, "cancelled"));
    });

    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });

  return { promise, abort: () => xhr.abort() };
}

export function messageFrom(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
