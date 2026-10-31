// Rules tab: the rule graph on a React Flow canvas. Edits stay local until Save, which the server validates with
// the same loader the CLI uses; Preview runs a dry run of the edited graph without saving it.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api, type Playlist } from "./api";
import {
  FIELD_CHOICES,
  fromFlow,
  newNodeId,
  NODE_SPECS,
  specFor,
  toFlow,
  type FieldSpec,
  type GraphDoc,
  type Params,
  type RuleNode,
} from "./graphModel";

type Sheet = "add" | "node" | "settings" | "result" | null;

interface Result {
  title: string;
  errors: string[];
  warnings: string[];
  report?: string;
}

const NAMED_NODE = /node '([^']+)'|(?:source|destination) '([^']+)'/g;

function namedNodes(errors: string[]): Set<string> {
  const out = new Set<string>();
  for (const e of errors) for (const m of e.matchAll(NAMED_NODE)) out.add(m[1] ?? m[2]);
  return out;
}

function summary(type: string, p: Params): string {
  const list = (v: unknown) => (Array.isArray(v) ? v.join(", ") : "");
  switch (type) {
    case "source.channel": return String(p.channel_id ?? "");
    case "source.channel_group": return `${(p.channel_ids as unknown[] | undefined)?.length ?? 0} channels`;
    case "filter.keyword": return [list(p.include) && `+ ${list(p.include)}`, list(p.exclude) && `− ${list(p.exclude)}`].filter(Boolean).join("  ");
    case "filter.regex": return String(p.pattern ?? "");
    case "filter.duration": {
      const lo = p.min_seconds as number | undefined, hi = p.max_seconds as number | undefined;
      return [lo !== undefined && `≥ ${lo}s`, hi !== undefined && `≤ ${hi}s`].filter(Boolean).join(", ");
    }
    case "filter.shorts": return p.mode === "only" ? "Shorts only" : "No Shorts";
    case "filter.age": return p.max_days !== undefined ? `≤ ${p.max_days} days old` : "";
    case "destination.playlist": return String(p.title ?? p.playlist_id ?? "");
    default: return "";
  }
}

function RuleNodeView({ id, data, selected }: NodeProps<RuleNode>) {
  const spec = specFor(data.type);
  const group = spec?.group ?? "filter";
  const bad = data.bad === true;
  return (
    <div className={`rnode ${group}${selected ? " selected" : ""}${bad ? " bad" : ""}${spec?.reserved ? " reserved" : ""}`}>
      {group !== "source" && <Handle type="target" position={Position.Top} />}
      <div className="rnode-kind">{spec?.label ?? data.type}</div>
      <div className="rnode-id">{id}</div>
      {summary(data.type, data.params) && <div className="rnode-sum">{summary(data.type, data.params)}</div>}
      {group !== "destination" && <Handle type="source" position={Position.Bottom} />}
    </div>
  );
}

const nodeTypes = { rule: RuleNodeView };

export default function Rules({ active }: { active: boolean }) {
  return (
    <ReactFlowProvider>
      <Editor active={active} />
    </ReactFlowProvider>
  );
}

function Editor({ active }: { active: boolean }) {
  const flow = useReactFlow<RuleNode, Edge>();
  const canvas = useRef<HTMLDivElement>(null);
  const [nodes, setNodes] = useState<RuleNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [settings, setSettings] = useState<GraphDoc["settings"]>({ multi_match: "all" });
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [sheet, setSheet] = useState<Sheet>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState<"save" | "preview" | null>(null);
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [badNodes, setBadNodes] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const { graph } = await api.graph();
      if (graph) {
        const f = toFlow(graph);
        setNodes(f.nodes);
        setEdges(f.edges);
        setSettings(graph.settings);
      } else {
        setNodes([]);
        setEdges([]);
        setSettings({ multi_match: "all" });
      }
      setDirty(false);
      setBadNodes(new Set());
      setLoaded(true);
      requestAnimationFrame(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, [flow]);

  useEffect(() => {
    load();
    api.playlists().then(setPlaylists, () => setPlaylists(null)); // without it, playlist ids are typed by hand
  }, [load]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (active && loaded) requestAnimationFrame(() => flow.fitView({ padding: 0.2, maxZoom: 1 }));
    // only when the tab is shown again; refitting on every edit would fight the user
  }, [active]);

  const shownNodes = useMemo(
    () => nodes.map((n) => (badNodes.has(n.id) ? { ...n, data: { ...n.data, bad: true } } : n)),
    [nodes, badNodes],
  );

  const onNodesChange = useCallback((changes: NodeChange<RuleNode>[]) => {
    setNodes((ns) => applyNodeChanges(changes, ns));
    if (changes.some((c) => c.type === "position" || c.type === "remove" || c.type === "add" || c.type === "replace")) {
      setDirty(true);
    }
    for (const c of changes) if (c.type === "remove") setSelected((s) => (s === c.id ? null : s));
  }, []);

  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((es) => applyEdgeChanges(changes, es));
    if (changes.some((c) => c.type === "remove" || c.type === "add")) setDirty(true);
  }, []);

  const typeOf = useCallback((id: string) => nodes.find((n) => n.id === id)?.data.type ?? "", [nodes]);

  const isValidConnection = useCallback((c: Connection | Edge) => {
    if (!c.source || !c.target || c.source === c.target) return false;
    if (specFor(typeOf(c.source))?.group === "destination") return false;
    if (specFor(typeOf(c.target))?.group === "source") return false;
    return !edges.some((e) => e.source === c.source && e.target === c.target);
  }, [edges, typeOf]);

  const onConnect = useCallback((c: Connection) => {
    setEdges((es) => addEdge({ ...c, id: `${c.source}->${c.target}` }, es));
    setDirty(true);
  }, []);

  function addNode(type: string) {
    const spec = specFor(type)!;
    const id = newNodeId(type, nodes.map((n) => n.id));
    const rect = canvas.current?.getBoundingClientRect();
    const center = rect
      ? flow.screenToFlowPosition({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 3 })
      : { x: 0, y: 0 };
    const node: RuleNode = {
      id, type: "rule", position: { x: center.x - 80, y: center.y - 30 },
      data: { type, params: structuredClone(spec.defaults) },
    };
    setNodes((ns) => [...ns, node]);
    setDirty(true);
    setSelected(id);
    setSheet("node");
  }

  function updateParams(id: string, params: Params) {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, params } } : n)));
    setDirty(true);
  }

  function renameNode(from: string, to: string): string | null {
    if (to === from) return null;
    if (!to.trim()) return "The id can't be empty.";
    if (nodes.some((n) => n.id === to)) return `There is already a node called ${to}.`;
    setNodes((ns) => ns.map((n) => (n.id === from ? { ...n, id: to } : n)));
    setEdges((es) => es.map((e) => {
      const source = e.source === from ? to : e.source;
      const target = e.target === from ? to : e.target;
      return { ...e, id: `${source}->${target}`, source, target };
    }));
    setSelected(to);
    setDirty(true);
    return null;
  }

  function deleteNode(id: string) {
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
    setSelected(null);
    setSheet(null);
    setDirty(true);
  }

  function deleteEdge(id: string) {
    setEdges((es) => es.filter((e) => e.id !== id));
    setSelectedEdge(null);
    setDirty(true);
  }

  const doc = () => fromFlow(nodes, edges, settings);

  async function preview() {
    setBusy("preview");
    try {
      const { report } = await api.preview(doc());
      setBadNodes(new Set());
      setResult({ title: "Preview (dry run, nothing saved)", errors: [], warnings: [], report });
    } catch (err) {
      const errors = errorsOf(err);
      setBadNodes(namedNodes(errors));
      setResult({ title: "Preview refused", errors, warnings: [] });
    } finally {
      setBusy(null);
      setSheet("result");
    }
  }

  async function save() {
    setBusy("save");
    try {
      const { warnings } = await api.saveGraph(doc());
      setDirty(false);
      setBadNodes(new Set());
      if (warnings.length) {
        setResult({ title: "Saved, with warnings", errors: [], warnings });
        setSheet("result");
      } else {
        setResult({ title: "Saved", errors: [], warnings: [] });
        setSheet(null);
      }
    } catch (err) {
      const errors = errorsOf(err);
      setBadNodes(namedNodes(errors));
      setResult({ title: `Not saved: ${errors.length} problem${errors.length === 1 ? "" : "s"}`, errors, warnings: [] });
      setSheet("result");
    } finally {
      setBusy(null);
    }
  }

  const node = nodes.find((n) => n.id === selected) ?? null;

  return (
    <div className="rules">
      <header className="bar rules-bar">
        <h1>Rules{dirty && <span className="dirty" title="Unsaved changes"> •</span>}</h1>
        <div className="bar-actions">
          <button type="button" className="icon-btn" aria-label="Add a node" onClick={() => setSheet("add")}>＋</button>
          <button type="button" className="icon-btn" aria-label="Graph settings" onClick={() => setSheet("settings")}>⚙</button>
          <button type="button" className="btn" onClick={preview} disabled={!loaded || busy !== null || !nodes.length}>
            {busy === "preview" ? "Running…" : "Preview"}
          </button>
          <button type="button" className="btn primary" onClick={save} disabled={!loaded || busy !== null || !dirty}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
        </div>
      </header>
      {dirty && <p className="sr-only" role="status">Unsaved changes</p>}
      {result?.title === "Saved" && !dirty && (
        <p className="toast" role="status" onAnimationEnd={() => setResult(null)}>Saved</p>
      )}
      {loadError && (
        <div className="notice error" role="alert">
          <p>{loadError}</p>
          <button type="button" className="btn" onClick={load}>Try again</button>
        </div>
      )}
      {selectedEdge && (
        <div className="edge-bar">
          <span>Link selected</span>
          <button type="button" className="btn danger" onClick={() => deleteEdge(selectedEdge)}>Delete link</button>
        </div>
      )}
      <div className="canvas" ref={canvas}>
        <ReactFlow
          nodes={shownNodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          onNodeClick={(_, n) => {
            setSelected(n.id);
            setSelectedEdge(null);
            setSheet("node");
          }}
          onEdgeClick={(_, e) => {
            setSelectedEdge(e.id);
            setSelected(null);
          }}
          onPaneClick={() => {
            setSelectedEdge(null);
            if (sheet === "node") setSheet(null);
          }}
          defaultEdgeOptions={{ markerEnd: { type: MarkerType.ArrowClosed } }}
          minZoom={0.2}
          maxZoom={2}
        >
          <Background gap={20} />
          <Controls showInteractive={false} position="top-right" />
        </ReactFlow>
        {loaded && nodes.length === 0 && (
          <div className="canvas-empty">
            <p>No rules yet.</p>
            <p className="muted small">Add a source, some filters and a playlist, then drag (or tap) from a node's bottom dot to the next node's top dot.</p>
            <button type="button" className="btn primary" onClick={() => setSheet("add")}>Add a node</button>
          </div>
        )}
      </div>

      {sheet === "add" && (
        <SheetPanel title="Add a node" onClose={() => setSheet(null)}>
          {(["source", "filter", "destination"] as const).map((group) => (
            <div key={group} className="palette-group">
              <h3>{group === "source" ? "Sources" : group === "filter" ? "Filters" : "Destination"}</h3>
              <div className="palette">
                {NODE_SPECS.filter((s) => s.group === group).map((s) => (
                  <button
                    key={s.type}
                    type="button"
                    className={`chip-btn ${group}`}
                    disabled={s.reserved}
                    title={s.reserved ? "Not available yet" : s.type}
                    onClick={() => addNode(s.type)}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </SheetPanel>
      )}

      {sheet === "settings" && (
        <SheetPanel title="Graph settings" onClose={() => setSheet(null)}>
          <label className="field">
            <span>When a video matches several playlists</span>
            <select
              value={String(settings?.multi_match ?? "all")}
              onChange={(e) => {
                setSettings({ ...(settings ?? {}), multi_match: e.target.value });
                setDirty(true);
              }}
            >
              <option value="all">File it into every matching playlist</option>
              <option value="first">File it into the first one only</option>
            </select>
          </label>
        </SheetPanel>
      )}

      {sheet === "node" && node && (
        <SheetPanel title={specFor(node.data.type)?.label ?? node.data.type} onClose={() => setSheet(null)}>
          <NodeForm
            key={node.id}
            node={node}
            playlists={playlists}
            onParams={(p) => updateParams(node.id, p)}
            onRename={(to) => renameNode(node.id, to)}
            onDelete={() => deleteNode(node.id)}
          />
        </SheetPanel>
      )}

      {sheet === "result" && result && (
        <SheetPanel title={result.title} onClose={() => setSheet(null)}>
          {result.errors.length > 0 && (
            <ul className="error-list" role="alert">{result.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
          {result.errors.length > 0 && <p className="muted small">Your edits are kept. Fix these and save again.</p>}
          {result.warnings.length > 0 && (
            <ul className="warn-list">{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          )}
          {result.report !== undefined && <pre className="report">{result.report}</pre>}
        </SheetPanel>
      )}
    </div>
  );
}

function errorsOf(err: unknown): string[] {
  if (err && typeof err === "object" && "errors" in err && Array.isArray(err.errors) && err.errors.length) {
    return err.errors.map(String);
  }
  return [err instanceof Error ? err.message : String(err)];
}

function SheetPanel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <aside className="sheet" role="dialog" aria-label={title}>
      <div className="sheet-head">
        <h2>{title}</h2>
        <button type="button" className="icon-btn" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <div className="sheet-body">{children}</div>
    </aside>
  );
}

function NodeForm({ node, playlists, onParams, onRename, onDelete }: {
  node: RuleNode;
  playlists: Playlist[] | null;
  onParams: (p: Params) => void;
  onRename: (to: string) => string | null;
  onDelete: () => void;
}) {
  const spec = specFor(node.data.type);
  const params = node.data.params;
  const [id, setId] = useState(node.id);
  const [idError, setIdError] = useState<string | null>(null);

  const set = (key: string, value: unknown) => {
    const next = { ...params };
    if (value === undefined) delete next[key];
    else next[key] = value;
    onParams(next);
  };

  const commitId = () => {
    const err = onRename(id.trim());
    setIdError(err);
    if (err) setId(node.id);
  };

  return (
    <div className="node-form">
      <label className="field">
        <span>Name (used in error messages)</span>
        <input
          value={id}
          onChange={(e) => setId(e.target.value)}
          onBlur={commitId}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
        {idError && <small className="field-error" role="alert">{idError}</small>}
      </label>
      {!spec && <p className="notice error">Unknown node type {node.data.type}. Delete it to save.</p>}
      {spec?.reserved && <p className="notice info">This node type is reserved for a later version; a graph containing it can't run.</p>}
      {spec && !spec.reserved && spec.fields.length === 0 && <p className="muted small">No settings for this node.</p>}
      {spec?.fields.map((f) => (
        <Field key={f.key} spec={f} params={params} playlists={playlists} set={set} onParams={onParams} />
      ))}
      <div className="actions">
        <button type="button" className="btn danger" onClick={onDelete}>Delete node</button>
      </div>
    </div>
  );
}

function Field({ spec, params, playlists, set, onParams }: {
  spec: FieldSpec;
  params: Params;
  playlists: Playlist[] | null;
  set: (key: string, value: unknown) => void;
  onParams: (p: Params) => void;
}) {
  const value = params[spec.key];
  const help = spec.help && <small className="muted">{spec.help}</small>;
  switch (spec.kind) {
    case "text":
      return (
        <label className="field">
          <span>{spec.label}</span>
          <input
            value={typeof value === "string" ? value : ""}
            onChange={(e) => set(spec.key, e.target.value)}
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
          />
          {help}
        </label>
      );
    case "lines":
      return <LinesField spec={spec} value={value} set={set} />;
    case "fields": {
      const chosen = Array.isArray(value) ? (value as string[]) : (spec.default as string[]);
      return (
        <fieldset className="field">
          <legend>{spec.label}</legend>
          <div className="checks">
            {FIELD_CHOICES.map((c) => (
              <label key={c} className="check">
                <input
                  type="checkbox"
                  checked={chosen.includes(c)}
                  onChange={(e) => set(spec.key, e.target.checked
                    ? FIELD_CHOICES.filter((x) => x === c || chosen.includes(x))
                    : chosen.filter((x) => x !== c))}
                />
                {c}
              </label>
            ))}
          </div>
        </fieldset>
      );
    }
    case "bool":
      return (
        <label className="check field">
          <input
            type="checkbox"
            checked={typeof value === "boolean" ? value : Boolean(spec.default)}
            onChange={(e) => set(spec.key, e.target.checked)}
          />
          {spec.label}
        </label>
      );
    case "int":
    case "number":
      return <NumberField spec={spec} value={value} set={set} />;
    case "select":
      return (
        <label className="field">
          <span>{spec.label}</span>
          <select value={String(value ?? "")} onChange={(e) => set(spec.key, e.target.value)}>
            {spec.options!.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </label>
      );
    case "playlist": {
      const id = typeof value === "string" ? value : "";
      if (!playlists) {
        return (
          <label className="field">
            <span>Playlist id</span>
            <input value={id} onChange={(e) => set(spec.key, e.target.value)} autoCapitalize="off" spellCheck={false} />
            <small className="muted">Couldn't load your playlists, so type the id.</small>
          </label>
        );
      }
      const known = playlists.some((p) => p.id === id);
      return (
        <label className="field">
          <span>{spec.label}</span>
          <select
            value={id}
            onChange={(e) => {
              const p = playlists.find((x) => x.id === e.target.value);
              const next: Params = { ...params, playlist_id: e.target.value };
              if (p) next.title = p.title;
              else delete next.title;
              onParams(next);
            }}
          >
            <option value="">Pick a playlist…</option>
            {id && !known && <option value={id}>{String(params.title ?? id)} (not one of yours)</option>}
            {playlists.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}
          </select>
        </label>
      );
    }
  }
}

function LinesField({ spec, value, set }: { spec: FieldSpec; value: unknown; set: (k: string, v: unknown) => void }) {
  // the draft keeps blank lines while typing; the saved list drops them
  const [draft, setDraft] = useState(Array.isArray(value) ? value.join("\n") : "");
  return (
    <label className="field">
      <span>{spec.label}</span>
      <textarea
        rows={3}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          const items = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean);
          set(spec.key, spec.optional && !items.length ? undefined : items);
        }}
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
      />
      {spec.help && <small className="muted">{spec.help}</small>}
    </label>
  );
}

function NumberField({ spec, value, set }: { spec: FieldSpec; value: unknown; set: (k: string, v: unknown) => void }) {
  const [draft, setDraft] = useState(typeof value === "number" ? String(value) : "");
  return (
    <label className="field">
      <span>{spec.label}</span>
      <input
        type="text"
        inputMode={spec.kind === "int" ? "numeric" : "decimal"}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          const t = e.target.value.trim();
          if (!t) {
            set(spec.key, undefined);
            return;
          }
          const n = spec.kind === "int" ? Number.parseInt(t, 10) : Number.parseFloat(t);
          if (Number.isFinite(n) && (spec.kind !== "int" || /^\d+$/.test(t))) set(spec.key, n);
        }}
      />
      {spec.optional && <small className="muted">Leave empty for no limit.</small>}
    </label>
  );
}
