// The rule graph as the server stores it (the YAML document) and as React Flow draws it, plus the node catalogue
// the editor's palette and forms are built from.
import type { Edge, Node } from "@xyflow/react";

export type Params = Record<string, unknown>;

export interface GraphNode {
  id: string;
  type: string;
  params?: Params;
  ui?: { x: number; y: number };
}

export interface GraphEdge {
  from: string;
  to: string;
}

export interface GraphDoc {
  version: number;
  settings?: Record<string, unknown>;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface RuleData extends Record<string, unknown> {
  type: string;
  params: Params;
}

export type RuleNode = Node<RuleData, "rule">;

// phones are portrait, so the graph flows top to bottom: one row per depth, siblings side by side
export const COLUMN = 200;
export const ROW = 120;

function edgeId(from: string, to: string): string {
  return `${from}->${to}`;
}

/** Row = longest path from a node with no incoming edge, so the graph reads top to bottom. */
function autoLayout(doc: GraphDoc): Map<string, { x: number; y: number }> {
  const ids = new Set(doc.nodes.map((n) => n.id));
  const incoming = new Map<string, string[]>();
  for (const e of doc.edges) {
    if (ids.has(e.from) && ids.has(e.to)) incoming.set(e.to, [...(incoming.get(e.to) ?? []), e.from]);
  }
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  const depthOf = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0; // a cycle; the loader will name it on save
    visiting.add(id);
    const d = Math.max(-1, ...(incoming.get(id) ?? []).map(depthOf)) + 1;
    visiting.delete(id);
    depth.set(id, d);
    return d;
  };
  const used = new Map<number, number>(); // depth -> nodes placed in that row so far
  const out = new Map<string, { x: number; y: number }>();
  for (const n of doc.nodes) {
    const row = depthOf(n.id);
    const col = used.get(row) ?? 0;
    used.set(row, col + 1);
    out.set(n.id, { x: col * COLUMN, y: row * ROW });
  }
  return out;
}

export function toFlow(doc: GraphDoc): { nodes: RuleNode[]; edges: Edge[] } {
  const layout = autoLayout(doc);
  const nodes: RuleNode[] = doc.nodes.map((n) => ({
    id: n.id,
    type: "rule",
    position: n.ui ? { x: n.ui.x, y: n.ui.y } : layout.get(n.id)!,
    data: { type: n.type, params: { ...(n.params ?? {}) } },
  }));
  const edges: Edge[] = doc.edges.map((e) => ({ id: edgeId(e.from, e.to), source: e.from, target: e.to }));
  return { nodes, edges };
}

/** `version` is the loaded graph's, so saving never rewrites it; a new graph is version 1. */
export function fromFlow(nodes: RuleNode[], edges: Edge[], settings: GraphDoc["settings"], version = 1): GraphDoc {
  const ids = new Set(nodes.map((n) => n.id));
  const seen = new Set<string>();
  const outEdges: GraphEdge[] = [];
  for (const e of edges) {
    const key = edgeId(e.source, e.target);
    if (!ids.has(e.source) || !ids.has(e.target) || seen.has(key)) continue;
    seen.add(key);
    outEdges.push({ from: e.source, to: e.target });
  }
  const outNodes: GraphNode[] = nodes.map((n) => {
    const node: GraphNode = { id: n.id, type: n.data.type };
    if (Object.keys(n.data.params).length) node.params = { ...n.data.params };
    node.ui = { x: Math.round(n.position.x), y: Math.round(n.position.y) };
    return node;
  });
  // key order matches the YAML files: version, settings, nodes, edges
  return settings === undefined
    ? { version, nodes: outNodes, edges: outEdges }
    : { version, settings, nodes: outNodes, edges: outEdges };
}

// --- node catalogue ---

export type FieldKind = "text" | "lines" | "fields" | "bool" | "int" | "number" | "select" | "playlist";

export interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  options?: string[];
  default?: unknown; // the loader's default, shown when the key is absent
  optional?: boolean; // an empty input removes the key
  help?: string;
}

export interface NodeSpec {
  type: string;
  label: string;
  group: "source" | "filter" | "destination";
  fields: FieldSpec[];
  defaults: Params;
  reserved?: boolean;
}

const TEXT_FIELDS = ["title", "description", "tags"];

export const NODE_SPECS: NodeSpec[] = [
  {
    type: "source.channel", label: "Channel", group: "source", defaults: { channel_id: "" },
    fields: [{ key: "channel_id", label: "Channel id", kind: "text", help: "Starts with UC, 24 characters" }],
  },
  {
    type: "source.channel_group", label: "Channel group", group: "source", defaults: { channel_ids: [] },
    fields: [{ key: "channel_ids", label: "Channel ids", kind: "lines", help: "One per line" }],
  },
  { type: "source.subscriptions", label: "My subscriptions", group: "source", defaults: {}, fields: [] },
  {
    type: "filter.keyword", label: "Keywords", group: "filter", defaults: { include: [] },
    fields: [
      { key: "include", label: "Include any of", kind: "lines", optional: true, help: "One term per line" },
      { key: "exclude", label: "Drop if any of", kind: "lines", optional: true, help: "One term per line" },
      { key: "fields", label: "Look in", kind: "fields", default: ["title", "description"] },
      { key: "whole_word", label: "Whole words only", kind: "bool", default: true },
    ],
  },
  {
    type: "filter.regex", label: "Regex", group: "filter", defaults: { pattern: "" },
    fields: [
      { key: "pattern", label: "Pattern", kind: "text" },
      { key: "fields", label: "Look in", kind: "fields", default: ["title", "description"] },
      { key: "ignore_case", label: "Ignore case", kind: "bool", default: true },
      { key: "negate", label: "Drop on match", kind: "bool", default: false },
    ],
  },
  {
    type: "filter.duration", label: "Duration", group: "filter", defaults: { min_seconds: 60 },
    fields: [
      { key: "min_seconds", label: "Minimum seconds", kind: "int", optional: true },
      { key: "max_seconds", label: "Maximum seconds", kind: "int", optional: true },
    ],
  },
  {
    type: "filter.shorts", label: "Shorts", group: "filter", defaults: { mode: "exclude" },
    fields: [{ key: "mode", label: "Mode", kind: "select", options: ["exclude", "only"] }],
  },
  {
    type: "filter.age", label: "Age", group: "filter", defaults: { max_days: 7 },
    fields: [{ key: "max_days", label: "Newer than (days)", kind: "number" }],
  },
  { type: "filter.live", label: "Not live", group: "filter", defaults: {}, fields: [] },
  { type: "filter.llm", label: "LLM filter (coming later)", group: "filter", defaults: {}, fields: [], reserved: true },
  { type: "router.llm", label: "LLM router (coming later)", group: "filter", defaults: {}, fields: [], reserved: true },
  {
    type: "destination.playlist", label: "Playlist", group: "destination", defaults: { playlist_id: "" },
    fields: [{ key: "playlist_id", label: "Playlist", kind: "playlist" }],
  },
];

export const FIELD_CHOICES = TEXT_FIELDS;

export function specFor(type: string): NodeSpec | undefined {
  return NODE_SPECS.find((s) => s.type === type);
}

/** A readable id not already taken, e.g. "keyword_2". */
export function newNodeId(type: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = type.split(".").pop()!;
  for (let i = 1; ; i++) {
    const id = `${base}_${i}`;
    if (!used.has(id)) return id;
  }
}
