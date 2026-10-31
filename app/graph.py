"""Rule graph: load, validate and execute (sources -> filters -> destination playlists).

Executor semantics: a video enters at every source node whose channels include its
channel, then follows edges depth-first in sorted target order. A filter passes or drops
the video; a destination files it. With multi_match "all" every destination reached is
collected (deduplicated); with "first" the walk stops at the first destination reached.
The executor performs no YouTube calls.
"""
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Literal

import yaml
from pydantic import ValidationError

from app.detect import Video
from app.filters import FILTERS, LlmRouterParams, Params, Predicate

SUPPORTED_VERSION = 1
RESERVED_TYPES = {"filter.llm", "router.llm"}
_PLAYLIST_PL = re.compile(r"PL[A-Za-z0-9_-]+")
_PLAYLIST_ANY = re.compile(r"[A-Za-z0-9_-]{10,}")
_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")


class GraphError(ValueError):
    """The graph is invalid; `errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid graph:\n- " + "\n- ".join(errors))


class NotImplementedInThisVersion(Exception):
    """A graph uses a reserved node type (LLM filter/router) that does nothing yet."""


class SourceChannelParams(Params):
    channel_id: str


class SourceGroupParams(Params):
    channel_ids: list[str]


class SourceSubscriptionsParams(Params):
    pass


class DestinationParams(Params):
    playlist_id: str
    title: str | None = None


SOURCES: dict[str, type[Params]] = {
    "source.channel": SourceChannelParams,
    "source.channel_group": SourceGroupParams,
    "source.subscriptions": SourceSubscriptionsParams,
}
PARAM_MODELS: dict[str, type[Params]] = {
    **SOURCES,
    **{t: s.params for t, s in FILTERS.items()},
    "router.llm": LlmRouterParams,
    "destination.playlist": DestinationParams,
}


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    params: Params


@dataclass
class Graph:
    multi_match: Literal["all", "first"]
    nodes: dict[str, Node]
    edges: dict[str, list[str]]  # node id -> sorted, unique target ids
    warnings: list[str] = field(default_factory=list)


def _fmt(e: ValidationError) -> str:
    return "; ".join(
        (".".join(str(p) for p in x["loc"]) + ": " if x["loc"] else "") + x["msg"] for x in e.errors()
    )


def _check_channel(cid: str) -> str | None:
    return None if _CHANNEL.fullmatch(cid) else f"channel id {cid!r} must start with 'UC' and be 24 characters"


def parse_graph(data: Any) -> Graph:
    """Validate a decoded graph document and return the Graph; raises GraphError listing all problems."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        raise GraphError(["top level must be a mapping"])
    for key in sorted(set(data) - {"version", "settings", "nodes", "edges"}, key=str):
        errors.append(f"unknown top-level key {key!r}")
    version = data.get("version")
    if type(version) is not int or version != SUPPORTED_VERSION:
        errors.append(f"version must be {SUPPORTED_VERSION}, got {data.get('version')!r}")

    settings = data.get("settings") or {}
    multi_match = "all"
    if not isinstance(settings, dict):
        errors.append("settings must be a mapping")
    else:
        for key in sorted(set(settings) - {"multi_match"}, key=str):
            errors.append(f"unknown setting {key!r}")
        multi_match = settings.get("multi_match", "all")
        if multi_match not in ("all", "first"):
            errors.append(f"settings.multi_match must be 'all' or 'first', got {multi_match!r}")
            multi_match = "all"

    nodes: dict[str, Node] = {}
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("nodes must be a non-empty list")
        raw_nodes = []
    for i, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            errors.append(f"nodes[{i}] must be a mapping")
            continue
        nid, ntype = raw.get("id"), raw.get("type")
        if not isinstance(nid, str) or not nid.strip():
            errors.append(f"nodes[{i}] needs a non-empty string id")
            continue
        if nid in nodes:
            errors.append(f"node {nid!r}: duplicate id")
            continue
        for key in sorted(set(raw) - {"id", "type", "params", "ui"}, key=str):  # ui: editor positions, ignored
            errors.append(f"node {nid!r}: unknown key {key!r}")
        model = PARAM_MODELS.get(ntype) if isinstance(ntype, str) else None
        if model is None:
            errors.append(f"node {nid!r}: unknown type {ntype!r}")
            continue
        raw_params = raw.get("params") or {}
        if not isinstance(raw_params, dict):
            errors.append(f"node {nid!r}: params must be a mapping")
            continue
        try:
            params = model.model_validate(raw_params)
        except ValidationError as e:
            errors.append(f"node {nid!r} ({ntype}): {_fmt(e)}")
            continue
        _check_node_values(nid, params, errors, warnings)
        nodes[nid] = Node(nid, ntype, params)

    edges: dict[str, set[str]] = {nid: set() for nid in nodes}
    raw_edges = data.get("edges", [])
    if not isinstance(raw_edges, list):
        errors.append("edges must be a list")
        raw_edges = []
    for i, e in enumerate(raw_edges):
        if not isinstance(e, dict) or set(e) != {"from", "to"}:
            errors.append(f"edges[{i}] must be a mapping with exactly 'from' and 'to'")
            continue
        src, dst = e["from"], e["to"]
        bad = False
        for end, nid in (("from", src), ("to", dst)):
            if not isinstance(nid, str):
                errors.append(f"edges[{i}]: '{end}' must be a node id string, got {nid!r}")
                bad = True
            elif nid not in nodes:
                errors.append(f"edges[{i}]: '{end}' references unknown node {nid!r}")
                bad = True
        if bad:
            continue
        if nodes[src].type == "destination.playlist":
            errors.append(f"edges[{i}]: destination {src!r} cannot have outgoing edges")
        elif nodes[dst].type in SOURCES:
            errors.append(f"edges[{i}]: source {dst!r} cannot have incoming edges")
        else:
            edges[src].add(dst)

    sorted_edges = {k: sorted(v) for k, v in edges.items()}
    cycles = _cycle_errors(sorted_edges)
    errors.extend(cycles)
    if not cycles:
        for n in nodes.values():
            if n.type in SOURCES and not _reaches_destination(n.id, sorted_edges, nodes):
                errors.append(f"source {n.id!r} does not reach any destination")
    targets = {t for ts in sorted_edges.values() for t in ts}
    for n in nodes.values():
        if n.type == "destination.playlist" and n.id not in targets:
            warnings.append(f"destination {n.id!r} has no incoming edges")
    if errors:
        raise GraphError(errors)
    return Graph(multi_match, nodes, sorted_edges, warnings)


def _check_node_values(nid: str, p: Params, errors: list[str], warnings: list[str]) -> None:
    if isinstance(p, SourceChannelParams):
        msg = _check_channel(p.channel_id)
        if msg:
            errors.append(f"node {nid!r}: {msg}")
    elif isinstance(p, SourceGroupParams):
        if not p.channel_ids:
            errors.append(f"node {nid!r}: channel_ids must not be empty")
        for cid in p.channel_ids:
            msg = _check_channel(cid)
            if msg:
                errors.append(f"node {nid!r}: {msg}")
    elif isinstance(p, DestinationParams):
        pid = p.playlist_id
        if _PLAYLIST_PL.fullmatch(pid):
            return
        if _PLAYLIST_ANY.fullmatch(pid):
            warnings.append(f"node {nid!r}: playlist id {pid!r} is not PL-prefixed")
        else:
            errors.append(f"node {nid!r}: playlist_id {pid!r} is not a valid playlist id")


def _cycle_errors(edges: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}  # 1 = on stack, 2 = done
    errors: list[str] = []
    for root in edges:
        if root in state:
            continue
        state[root] = 1
        path = [root]
        iters = [iter(edges[root])]
        while iters:
            for t in iters[-1]:
                if state.get(t) == 1:
                    errors.append("cycle: " + " -> ".join(path[path.index(t):] + [t]))
                elif t not in state:
                    state[t] = 1
                    path.append(t)
                    iters.append(iter(edges[t]))
                    break
            else:
                state[path.pop()] = 2
                iters.pop()
    return errors


def _reaches_destination(start: str, edges: dict[str, list[str]], nodes: dict[str, Node]) -> bool:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if nodes[n].type == "destination.playlist":
            return True
        stack.extend(edges[n])
    return False


def load_graph(path: str | Path) -> Graph:
    """Load a YAML or JSON graph file (yaml.safe_load reads both)."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise GraphError([f"cannot read {path}: {e}"]) from e
    except yaml.YAMLError as e:
        raise GraphError([f"cannot parse {path}: {e}"]) from e
    return parse_graph(data)


def _has_subscriptions(graph: Graph) -> bool:
    return any(isinstance(n.params, SourceSubscriptionsParams) for n in graph.nodes.values())


def required_channels(
    graph: Graph, subscriptions_resolver: Callable[[], Iterable[str]] | None = None
) -> set[str]:
    """Channel ids the graph needs. A resolver is required (ValueError otherwise) if there is a subscriptions source."""
    if subscriptions_resolver is None and _has_subscriptions(graph):
        raise ValueError("graph has a source.subscriptions node: a subscriptions resolver is required")
    out: set[str] = set()
    for n in graph.nodes.values():
        if isinstance(n.params, SourceChannelParams):
            out.add(n.params.channel_id)
        elif isinstance(n.params, SourceGroupParams):
            out.update(n.params.channel_ids)
        elif isinstance(n.params, SourceSubscriptionsParams):
            out.update(subscriptions_resolver())  # resolver is set here
    return out


@dataclass(frozen=True)
class TraceEntry:
    node_id: str
    outcome: Literal["pass", "drop", "file"]
    reason: str


@dataclass
class RunResult:
    filings: list[tuple[str, str]] = field(default_factory=list)  # (video_id, playlist_id), deduplicated
    traces: dict[str, list[TraceEntry]] = field(default_factory=dict)
    node_counts: dict[str, Counter] = field(default_factory=dict)  # node id -> Counter of outcomes


def _source_accepts(node: Node, channel_id: str, subscriptions: Collection[str] | None) -> bool:
    p = node.params
    if isinstance(p, SourceChannelParams):
        return p.channel_id == channel_id
    if isinstance(p, SourceGroupParams):
        return channel_id in p.channel_ids
    if isinstance(p, SourceSubscriptionsParams):
        return subscriptions is not None and channel_id in subscriptions
    return False


def execute(
    graph: Graph,
    videos: Iterable[Video],
    now: datetime,
    subscriptions: Collection[str] | None = None,
) -> RunResult:
    """Run videos through the graph.

    `subscriptions` is the resolved subscription channel set; it is required (ValueError if None)
    when the graph has a source.subscriptions node, so a forgotten set never files everything.
    Reserved nodes (filter.llm, router.llm) fail fast: NotImplementedInThisVersion is raised before
    any video is processed, even if the node is unreachable, because silently skipping an LLM
    filter would misfile videos. A repeated video_id in `videos` is processed once.
    """
    for nid in sorted(graph.nodes):
        n = graph.nodes[nid]
        if n.type in RESERVED_TYPES:
            raise NotImplementedInThisVersion(f"node {n.id!r} ({n.type}) is not implemented in this version")
    if subscriptions is None and _has_subscriptions(graph):
        raise ValueError("graph has a source.subscriptions node: pass the resolved subscriptions set")
    predicates: dict[str, Predicate] = {
        n.id: FILTERS[n.type].build(n.params) for n in graph.nodes.values() if n.type in FILTERS
    }
    sources = sorted(n.id for n in graph.nodes.values() if n.type in SOURCES)
    result = RunResult(node_counts={nid: Counter() for nid in graph.nodes})
    first = graph.multi_match == "first"

    for video in videos:
        if video.video_id in result.traces:
            continue
        trace: list[TraceEntry] = []
        files: list[str] = []
        done: set[str] = set()

        def record(nid: str, outcome: str, reason: str) -> None:
            trace.append(TraceEntry(nid, outcome, reason))
            result.node_counts[nid][outcome] += 1

        for sid in sources:
            if first and files:
                break
            if not _source_accepts(graph.nodes[sid], video.channel_id, subscriptions):
                continue
            done.add(sid)
            record(sid, "pass", f"channel {video.channel_id}")
            stack = list(reversed(graph.edges[sid]))
            while stack:
                nid = stack.pop()
                if nid in done or (first and files):
                    continue
                done.add(nid)
                node = graph.nodes[nid]
                if node.type == "destination.playlist":
                    files.append(node.params.playlist_id)
                    record(nid, "file", f"filed to {node.params.playlist_id}")
                    continue
                if nid in predicates:
                    ok, reason = predicates[nid](video, now)
                    record(nid, "pass" if ok else "drop", reason)
                    if not ok:
                        continue
                stack.extend(reversed(graph.edges[nid]))
        result.traces[video.video_id] = trace
        for pid in dict.fromkeys(files):
            result.filings.append((video.video_id, pid))
    return result
