// Queue tab: the queue playlist as compact rows. A row opens the video; ✓ removes it from the queue on YouTube.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, type Activity, type Playlist, type QueueItem, type QueueResponse } from "./api";
import { duration, relative, safeThumbnail } from "./format";

const POLL_MS = 3000;
const POLL_LIMIT_MS = 10 * 60 * 1000; // the button stops waiting on a check after 10 minutes

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// only https images from YouTube's hosts; anything else would also be blocked by the server's CSP
function thumbnail(item: QueueItem): string | undefined {
  return safeThumbnail(item.thumbnail) ?? undefined;
}

function tickError(err: unknown): string {
  if (err instanceof ApiError && err.status === 409) return "The queue changed. Refresh and try again.";
  return message(err); // 429 carries the server's quota message
}

interface RunState {
  running: boolean;
  text?: string;
  failed?: boolean;
}

function runResult(a: Activity): RunState {
  const last = a.last_manual;
  if (last?.status === "error") return { running: false, failed: true, text: `Check failed: ${last.message}` };
  if (last?.status === "done" && last.message.startsWith("No rules yet")) return { running: false, text: last.message };
  if (last?.status === "already_running") return { running: false, text: "Another run was in progress; showing its results." };
  const filed = Object.values(a.runs[0]?.summary?.filed ?? {}).reduce((s, n) => s + n, 0);
  return { running: false, text: filed ? `Done. Filed ${filed} video${filed === 1 ? "" : "s"}.` : "Done. Nothing new." };
}

export default function Queue({ active }: { active: boolean }) {
  const [data, setData] = useState<QueueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [run, setRun] = useState<RunState>({ running: false });
  const [changing, setChanging] = useState(false);
  const ticking = useRef(new Set<string>()); // item ids removed optimistically, waiting on the server
  const alive = useRef(true);
  const polling = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = await api.queue();
      if (!alive.current) return;
      setData({ ...q, items: q.items.filter((i) => !ticking.current.has(i.item_id)) });
      setLoadError(null);
    } catch (err) {
      if (alive.current) setLoadError(message(err));
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  const waitForRun = useCallback(async () => {
    if (polling.current) return;
    polling.current = true;
    const deadline = Date.now() + POLL_LIMIT_MS;
    try {
      for (;;) {
        await sleep(POLL_MS);
        if (!alive.current) return;
        const a = await api.activity();
        if (!a.running) {
          if (alive.current) setRun(runResult(a));
          break;
        }
        if (Date.now() >= deadline) {
          if (alive.current) {
            setRun({ running: false, failed: true, text: "The check is taking more than 10 minutes. See Activity for its result." });
          }
          break;
        }
      }
    } catch (err) {
      if (alive.current) setRun({ running: false, failed: true, text: `Lost track of the check: ${message(err)}` });
    } finally {
      polling.current = false;
    }
    await load();
  }, [load]);

  useEffect(() => {
    alive.current = true;
    load();
    // pick up a run started elsewhere (the timer, another tab) so the button reflects it
    api.activity().then((a) => {
      if (a.running && alive.current) {
        setRun({ running: true, text: "A check is running…" });
        waitForRun();
      }
    }, () => undefined);
    return () => {
      alive.current = false;
    };
  }, [load, waitForRun]);

  const wasActive = useRef(active);
  useEffect(() => {
    if (active && !wasActive.current) load();
    wasActive.current = active;
  }, [active, load]);

  async function checkNow() {
    setRun({ running: true, text: "Checking for new videos…" });
    try {
      const r = await api.run();
      if (r.status === "already_running") setRun({ running: true, text: "A check is already running; waiting for it…" });
    } catch (err) {
      setRun({ running: false, failed: true, text: `Check failed: ${message(err)}` });
      return;
    }
    await waitForRun();
  }

  async function tick(item: QueueItem) {
    if (!data) return;
    const index = data.items.findIndex((i) => i.item_id === item.item_id);
    ticking.current.add(item.item_id);
    setRowErrors(({ [item.item_id]: _, ...rest }) => rest);
    setData((d) => d && { ...d, items: d.items.filter((i) => i.item_id !== item.item_id) });
    try {
      await api.tick(item);
    } catch (err) {
      setData((d) => {
        if (!d || d.items.some((i) => i.item_id === item.item_id)) return d;
        const items = [...d.items];
        items.splice(Math.min(index, items.length), 0, item);
        return { ...d, items };
      });
      setRowErrors((e) => ({ ...e, [item.item_id]: tickError(err) }));
    } finally {
      ticking.current.delete(item.item_id);
    }
  }

  if (!data) {
    return (
      <div className="page">
        <header className="bar"><h1>Queue</h1></header>
        {loadError ? (
          <div className="notice error" role="alert">
            <p>{loadError}</p>
            <button type="button" className="btn" onClick={load}>Try again</button>
          </div>
        ) : (
          <p className="muted pad" aria-live="polite">{loading ? "Loading…" : ""}</p>
        )}
      </div>
    );
  }

  if (!data.playlist || changing) {
    return (
      <div className="page">
        <header className="bar"><h1>Queue</h1></header>
        <QueuePicker
          current={data.playlist?.id ?? null}
          onCancel={data.playlist ? () => setChanging(false) : undefined}
          onSaved={() => {
            setChanging(false);
            load();
          }}
        />
      </div>
    );
  }

  const count = data.items.length;
  return (
    <div className="page">
      <header className="bar queue-bar">
        <div className="bar-title">
          <h1>{data.playlist.title}</h1>
          <p className="muted small">
            {count} video{count === 1 ? "" : "s"}
            {" · "}
            <button type="button" className="link" onClick={() => setChanging(true)}>Change queue</button>
          </p>
        </div>
        <button type="button" className="btn primary" onClick={checkNow} disabled={run.running} aria-busy={run.running}>
          {run.running ? "Checking…" : "Check now"}
        </button>
      </header>

      {run.text && (
        <div className={`notice ${run.failed ? "error" : "info"}`} role={run.failed ? "alert" : "status"}>
          <p>{run.text}</p>
          {!run.running && (
            <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => setRun({ running: false })}>×</button>
          )}
        </div>
      )}
      {loadError && (
        <div className="notice error" role="alert">
          <p>{loadError}</p>
          <button type="button" className="btn" onClick={load}>Refresh</button>
        </div>
      )}

      {count === 0 ? (
        <p className="empty">The queue is empty. Press Check now to look for new videos.</p>
      ) : (
        <ul className="rows" aria-busy={loading}>
          {data.items.map((item) => (
            <li key={item.item_id} className="row-wrap">
              <div className="row">
                <a
                  className="row-link"
                  href={`https://www.youtube.com/watch?v=${encodeURIComponent(item.video_id)}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <span className="thumb">
                    {thumbnail(item) ? (
                      <img src={thumbnail(item)} alt="" loading="lazy" referrerPolicy="no-referrer" />
                    ) : (
                      <span className="thumb-blank" aria-hidden="true" />
                    )}
                    {item.duration_seconds !== null && (
                      <span className="dur">{duration(item.duration_seconds)}</span>
                    )}
                  </span>
                  <span className="row-text">
                    <span className="row-title">{item.title || "(untitled)"}</span>
                    <span className="row-meta">
                      {item.channel || "Unavailable video"}
                      {item.added_at && <> · {relative(item.added_at)}</>}
                    </span>
                  </span>
                </a>
                <button
                  type="button"
                  className="tick"
                  aria-label={`Done with ${item.title || "this video"}, remove it from the queue`}
                  onClick={() => tick(item)}
                >
                  ✓
                </button>
              </div>
              {rowErrors[item.item_id] && (
                <div className="row-error" role="alert">
                  <span>{rowErrors[item.item_id]}</span>
                  <button type="button" className="link" onClick={load}>Refresh</button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function QueuePicker({ current, onSaved, onCancel }: {
  current: string | null;
  onSaved: () => void;
  onCancel?: () => void;
}) {
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [selected, setSelected] = useState("");
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const fetchPlaylists = useCallback(() => {
    setError(null);
    api.playlists().then(setPlaylists, (err) => setError(message(err)));
  }, []);
  useEffect(fetchPlaylists, [fetchPlaylists]);

  const choice = playlists?.find((p) => p.id === selected);

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!choice) return;
    setSaving(true);
    setError(null);
    try {
      await api.setQueue(choice.id, typed);
      onSaved();
    } catch (err) {
      setError(message(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="card picker" onSubmit={save}>
      <h2>{current ? "Change the queue playlist" : "Choose your queue playlist"}</h2>
      <p className="muted small">
        Videos you tick are removed from this playlist on YouTube. Nothing is ever removed from any other playlist.
      </p>
      {error && <p className="notice error" role="alert">{error}</p>}
      {!playlists && !error && <p className="muted">Loading your playlists…</p>}
      {!playlists && error && <button type="button" className="btn" onClick={fetchPlaylists}>Try again</button>}
      {playlists && (
        <>
          <label className="field">
            <span>Playlist</span>
            <select
              value={selected}
              onChange={(e) => {
                setSelected(e.target.value);
                setTyped("");
              }}
            >
              <option value="">Pick a playlist…</option>
              {playlists.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title}{p.id === current ? " (current)" : ""}
                </option>
              ))}
            </select>
          </label>
          {choice && (
            <label className="field">
              <span>Type the playlist name to confirm: <strong>{choice.title}</strong></span>
              <input
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                autoComplete="off"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
              />
            </label>
          )}
          <div className="actions">
            {onCancel && <button type="button" className="btn" onClick={onCancel}>Cancel</button>}
            <button type="submit" className="btn primary" disabled={!choice || typed !== choice.title || saving}>
              {saving ? "Saving…" : "Use as queue"}
            </button>
          </div>
        </>
      )}
    </form>
  );
}
