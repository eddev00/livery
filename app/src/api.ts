// Calls into the engine through the preload bridge (the page never holds the token).
import type { LiveryBridge } from "../electron/preload";

declare global {
  interface Window { livery: LiveryBridge }
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public problems?: string[]) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = (await window.livery.call(method, path, body)) as { status: number; data: unknown };
  if (r.status >= 400) {
    const d = r.data as { detail?: string; problems?: string[] } | string;
    const msg = typeof d === "string" ? d : d?.detail ?? `request failed (${r.status})`;
    throw new ApiError(r.status, msg, typeof d === "object" ? d?.problems : undefined);
  }
  return r.data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
  pdf: async (path: string): Promise<Uint8Array | null> => (await window.livery.pdf(path)) as Uint8Array | null,
};

export const bridge = () => window.livery;
