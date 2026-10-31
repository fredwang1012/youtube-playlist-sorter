import { describe, expect, it } from "vitest";
import { fromFlow, toFlow, type GraphDoc } from "./graphModel";

// graphs/example.yaml, with editor positions added to every node
const example: GraphDoc = {
  version: 1,
  settings: { multi_match: "all" },
  nodes: [
    {
      id: "cooking_channels",
      type: "source.channel_group",
      params: { channel_ids: ["UCexample000000000000001", "UCexample000000000000002"] },
      ui: { x: 0, y: 0 },
    },
    { id: "no_shorts", type: "filter.shorts", params: { mode: "exclude" }, ui: { x: 220, y: 0 } },
    { id: "no_live", type: "filter.live", ui: { x: 440, y: 0 } },
    {
      id: "recipes",
      type: "filter.keyword",
      params: { include: ["recipe", "baking"], exclude: ["sponsored"], fields: ["title", "tags"] },
      ui: { x: 880, y: 40 },
    },
    { id: "recent", type: "filter.age", params: { max_days: 7 }, ui: { x: 660, y: -30 } },
    {
      id: "cooking_playlist",
      type: "destination.playlist",
      params: { playlist_id: "PLexampleExamplePlaylist01", title: "Cooking" },
      ui: { x: 1100, y: 0 },
    },
  ],
  edges: [
    { from: "cooking_channels", to: "no_shorts" },
    { from: "no_shorts", to: "no_live" },
    { from: "no_live", to: "recent" },
    { from: "recent", to: "recipes" },
    { from: "recipes", to: "cooking_playlist" },
  ],
};

describe("graphModel", () => {
  it("toFlow/fromFlow round-trip preserves nodes, params, edges, settings and ui positions", () => {
    const { nodes, edges } = toFlow(example);
    expect(nodes).toHaveLength(6);
    expect(edges).toHaveLength(5);
    expect(nodes.find((n) => n.id === "recent")?.position).toEqual({ x: 660, y: -30 });
    expect(fromFlow(nodes, edges, example.settings)).toEqual(example);
  });

  it("fromFlow drops edges to deleted nodes", () => {
    const { nodes, edges } = toFlow(example);
    const kept = nodes.filter((n) => n.id !== "recent");
    const graph = fromFlow(kept, edges, example.settings);
    expect(graph.nodes.map((n) => n.id)).not.toContain("recent");
    expect(graph.edges).toEqual([
      { from: "cooking_channels", to: "no_shorts" },
      { from: "no_shorts", to: "no_live" },
      { from: "recipes", to: "cooking_playlist" },
    ]);
  });

  it("toFlow lays out nodes without saved positions top to bottom", () => {
    const bare: GraphDoc = {
      version: 1,
      nodes: example.nodes.map(({ ui: _ui, ...n }) => n),
      edges: example.edges,
    };
    const { nodes } = toFlow(bare);
    const y = (id: string) => nodes.find((n) => n.id === id)!.position.y;
    expect(y("cooking_channels")).toBeLessThan(y("no_shorts"));
    expect(y("recipes")).toBeLessThan(y("cooking_playlist"));
    const back = fromFlow(nodes, toFlow(bare).edges, undefined);
    expect(back.settings).toBeUndefined();
    expect(back.nodes.every((n) => n.ui && Number.isInteger(n.ui.x))).toBe(true);
  });
});
