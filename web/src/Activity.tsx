// Activity tab: today's quota and the recent runs, newest first.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Activity as ActivityData, type RunRow } from "./api";
import { clock, relative } from "./format";

const POLL_MS = 3000;

function errorList(errors: string[] | number | undefined): { count: number; items: string[] } {
  if (typeof errors === "number") return { count: errors, items: [] };
  return { count: errors?.length ?? 0, items: errors ?? [] };
}

function QuotaBar({ used, budget, ceiling }: { used: number; budget: number; ceiling: number }) {
  const scale = Math.max(ceiling, used, 1);
  const pct = (n: number) => `${Math.min(100, (n / scale) * 100)}%`;
  const level = used >= ceiling ? "over" : used >= budget ? "headroom" : "ok";
  return (
    <div className="card">
      <div className="quota-head">
        <h2>Today's quota</h2>
        <span className="quota-num"><strong>{used.toLocaleString()}</strong> / {budget.toLocaleString()}</span>
      </div>
      <div
        className="quota-bar"
        role="meter"
        aria-label="YouTube quota used today"
        aria-valuemin={0}
        aria-valuemax={ceiling}
        aria-valuenow={used}
        aria-valuetext={`${used} of ${budget} units used; ticks may go up to ${ceiling}`}
      >
        <div className={`quota-fill ${level}`} style={{ width: pct(used) }} />
        <div className="quota-mark" style={{ left: pct(budget) }} aria-hidden="true" />
      </div>
      <p className="muted small">
        Runs stop at {budget.toLocaleString()} units. Ticks may use the headroom up to {ceiling.toLocaleString()}.
        Resets at midnight Pacific time.
      </p>
    </div>
  );
}

function Run({ run, titles }: { run: RunRow; titles: Record<string, string> }) {
  const s = run.summary;
  const errs = errorList(s?.errors);
  const filed = Object.entries(s?.filed ?? {}).filter(([, n]) => n > 0);
  const units = s?.units_run ?? s?.units;
  const trigger = s?.trigger ?? null; // rows from before triggers were recorded have none
  return (
    <li className="run">
      <div className="run-head">
        <time dateTime={run.started_at} title={clock(run.started_at)}>
          {clock(run.started_at)} <span className="muted">· {relative(run.started_at)}</span>
        </time>
        {trigger && <span className={`chip ${trigger}`}>{trigger}</span>}
      </div>
      {!s ? (
        <p className="muted small">{run.finished_at ? "Finished without a summary." : "Running, or stopped before it finished."}</p>
      ) : (
        <>
          <p className="small">
            {s.new_videos ?? 0} new
            {s.already ? ` · ${s.already} already filed` : ""}
            {s.deferred ? ` · ${s.deferred} deferred` : ""}
            {units !== undefined ? ` · ${units} units` : ""}
          </p>
          {filed.length > 0 ? (
            <ul className="filed">
              {filed.map(([pid, n]) => (
                <li key={pid}><span>{titles[pid] ?? pid}</span><strong>+{n}</strong></li>
              ))}
            </ul>
          ) : (
            <p className="muted small">Nothing filed.</p>
          )}
          {errs.count > 0 && (
            errs.items.length ? (
              <details className="errors">
                <summary>{errs.count} error{errs.count === 1 ? "" : "s"}</summary>
                <ul>{errs.items.map((e, i) => <li key={i}>{e}</li>)}</ul>
              </details>
            ) : (
              <p className="errors small">{errs.count} error{errs.count === 1 ? "" : "s"}</p>
            )
          )}
          {s.gave_up && s.gave_up.length > 0 && (
            <p className="muted small">Gave up on {s.gave_up.length} video{s.gave_up.length === 1 ? "" : "s"} after repeated failures.</p>
          )}
        </>
      )}
    </li>
  );
}

export default function Activity({ active }: { active: boolean }) {
  const [data, setData] = useState<ActivityData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const a = await api.activity();
      if (alive.current) {
        setData(a);
        setError(null);
      }
    } catch (err) {
      if (alive.current) setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    // destination titles from the saved graph label the per-playlist counts without spending quota
    api.graph().then(({ graph }) => {
      const t: Record<string, string> = {};
      for (const n of graph?.nodes ?? []) {
        const p = n.params as { playlist_id?: string; title?: string } | undefined;
        if (n.type === "destination.playlist" && p?.playlist_id && p.title) t[p.playlist_id] = p.title;
      }
      if (alive.current) setTitles(t);
    }, () => undefined);
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  useEffect(() => {
    if (!active || !data?.running) return;
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [active, data?.running, load]);

  const last = data?.last_manual;
  return (
    <div className="page">
      <header className="bar">
        <h1>Activity</h1>
        <button type="button" className="btn" onClick={load}>Refresh</button>
      </header>
      {error && <div className="notice error" role="alert"><p>{error}</p></div>}
      {!data && !error && <p className="muted pad">Loading…</p>}
      {data && (
        <>
          {data.running && <div className="notice info" role="status"><p>A run is in progress…</p></div>}
          {last?.status === "error" && (
            <div className="notice error" role="alert">
              <p>
                Last Check now failed{last.finished_at ? ` (${relative(last.finished_at)})` : ""}: {last.message}
              </p>
            </div>
          )}
          <QuotaBar used={data.quota.used_today} budget={data.quota.budget} ceiling={data.quota.tick_ceiling} />
          <h2 className="section">Recent runs</h2>
          {data.runs.length === 0 ? (
            <p className="empty">No runs yet.</p>
          ) : (
            <ul className="runs">{data.runs.map((r) => <Run key={r.id} run={r} titles={titles} />)}</ul>
          )}
        </>
      )}
    </div>
  );
}
