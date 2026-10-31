// Calls to the server's /api routes. Every call is same-origin with the session cookie; a 401 means the session
// is gone, so the page goes to the sign-in route instead of showing a broken view.
import type { GraphDoc } from "./graphModel";

export class ApiError extends Error {
  status: number;
  errors: string[];

  constructor(status: number, message: string, errors: string[] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

let redirecting = false;

function signIn(): void {
  if (!redirecting) {
    redirecting = true;
    window.location.assign("/auth/login");
  }
}

async function request<T>(method: "GET" | "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (method !== "GET") {
    // the server refuses writes that are not JSON
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body ?? {});
  }
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  if (res.status === 401) {
    signIn();
    throw new ApiError(401, "Signed out. Opening sign-in…");
  }
  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    // a non-JSON body (proxy error page); fall through to the status message
  }
  if (!res.ok) {
    const obj = (data ?? {}) as { error?: unknown; errors?: unknown };
    const errors = Array.isArray(obj.errors) ? obj.errors.map(String) : [];
    const message = typeof obj.error === "string" ? obj.error : errors[0] ?? `Request failed (HTTP ${res.status})`;
    throw new ApiError(res.status, message, errors);
  }
  return data as T;
}

export interface QueueItem {
  item_id: string;
  video_id: string;
  title: string;
  channel: string;
  duration_seconds: number | null;
  thumbnail: string | null;
  added_at: string;
  playlist_id: string;
}

export interface QueueResponse {
  playlist: { id: string; title: string } | null;
  items: QueueItem[];
}

export interface Playlist {
  id: string;
  title: string;
}

export interface RunSummary {
  trigger?: string | null;
  new_videos?: number;
  filed?: Record<string, number>;
  already?: number;
  deferred?: number;
  errors?: string[] | number; // older rows stored a count
  gave_up?: string[];
  units_run?: number;
  units_today?: number;
  units?: number; // older rows: units this run
  budget?: number;
}

export interface RunRow {
  id: number;
  started_at: string;
  finished_at: string | null;
  summary: RunSummary | null;
}

export interface Activity {
  running: boolean;
  runs: RunRow[];
  quota: { used_today: number; budget: number; tick_ceiling: number };
  last_manual: { status: "done" | "already_running" | "error"; message: string; finished_at: string | null } | null;
}

export interface Me {
  email: string;
  auth_mode: "google" | "tailscale";
}

export const api = {
  me: () => request<Me>("GET", "/api/me"),
  // ends the session on every device; the server answers 200 even when already signed out
  logout: () => request<{ ok: true }>("POST", "/auth/logout"),
  queue: () => request<QueueResponse>("GET", "/api/queue"),
  tick: (item: Pick<QueueItem, "item_id" | "video_id" | "playlist_id">) =>
    request<{ result: string }>("POST", "/api/queue/tick", {
      item_id: item.item_id, video_id: item.video_id, playlist_id: item.playlist_id,
    }),
  playlists: () => request<Playlist[]>("GET", "/api/playlists"),
  setQueue: (playlist_id: string, confirm_title: string) =>
    request<{ queue_playlist: string }>("PUT", "/api/settings/queue", { playlist_id, confirm_title }),
  run: () => request<{ status: "started" | "already_running" }>("POST", "/api/run"),
  activity: () => request<Activity>("GET", "/api/activity"),
  graph: () => request<{ graph: GraphDoc | null }>("GET", "/api/graph"),
  saveGraph: (graph: GraphDoc) => request<{ ok: true; warnings: string[] }>("PUT", "/api/graph", { graph }),
  preview: (graph: GraphDoc) => request<{ report: string }>("POST", "/api/graph/preview", { graph }),
};
