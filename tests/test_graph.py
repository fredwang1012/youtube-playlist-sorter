"""Graph validation, filters and executor."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.detect import Video
from app.graph import GraphError, NotImplementedInThisVersion, execute, load_graph, parse_graph, required_channels

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)
CH_A = "UC" + "a" * 22
CH_B = "UC" + "b" * 22
PL1 = "PLaaaaaaaaaa1"
PL2 = "PLaaaaaaaaaa2"


def vid(video_id="v1", channel=CH_A, **kw) -> Video:
    base = dict(
        video_id=video_id, channel_id=channel, title="Great recipe", description="", tags=(),
        category_id="22", live_broadcast_content="none", published_at=NOW - timedelta(days=1),
        duration_seconds=600, is_short=False,
    )
    base.update(kw)
    return Video(**base)


def graph(nodes, edges, **settings):
    return parse_graph({"version": 1, "settings": settings, "nodes": nodes, "edges": edges})


def src(i="s", ch=CH_A):
    return {"id": i, "type": "source.channel", "params": {"channel_id": ch}}


def dest(i="d", pl=PL1):
    return {"id": i, "type": "destination.playlist", "params": {"playlist_id": pl}}


def filt(i, typ, **params):
    return {"id": i, "type": typ, "params": params}


def chain(f_type, **params):
    """source -> filter -> destination graph."""
    return graph([src(), filt("f", f_type, **params), dest()],
                 [{"from": "s", "to": "f"}, {"from": "f", "to": "d"}])


def passes(g, v) -> bool:
    return bool(execute(g, [v], NOW).filings)


def test_example_loads():
    g = load_graph(Path(__file__).parent.parent / "graphs" / "example.yaml")
    assert g.multi_match == "all" and "cooking_playlist" in g.nodes


def test_json_accepted(tmp_path):
    p = tmp_path / "g.json"
    p.write_text('{"version":1,"nodes":[{"id":"s","type":"source.subscriptions"},'
                 '{"id":"d","type":"destination.playlist","params":{"playlist_id":"PLabc12345"}}],'
                 '"edges":[{"from":"s","to":"d"}]}')
    assert load_graph(p).nodes["s"].type == "source.subscriptions"


# ---- validation ----

def errors_of(data) -> str:
    with pytest.raises(GraphError) as ei:
        parse_graph(data)
    return "\n".join(ei.value.errors)


def test_unknown_type_and_param():
    msg = errors_of({"version": 1, "nodes": [{"id": "x", "type": "filter.nope"}], "edges": []})
    assert "'x'" in msg and "unknown type" in msg
    msg = errors_of({"version": 1, "edges": [], "nodes": [
        src(), dest(), filt("f", "filter.live", bogus=1)]})
    assert "'f'" in msg and "bogus" in msg


def test_duplicate_id_and_bad_version():
    msg = errors_of({"version": 2, "nodes": [src(), src()], "edges": []})
    assert "duplicate id" in msg and "version" in msg


def test_dangling_edge():
    msg = errors_of({"version": 1, "nodes": [src(), dest()],
                     "edges": [{"from": "s", "to": "d"}, {"from": "s", "to": "ghost"}]})
    assert "'ghost'" in msg


def test_cycle():
    msg = errors_of({"version": 1, "nodes": [src(), filt("a", "filter.live"), filt("b", "filter.live"), dest()],
                     "edges": [{"from": "s", "to": "a"}, {"from": "a", "to": "b"},
                               {"from": "b", "to": "a"}, {"from": "b", "to": "d"}]})
    assert "cycle: a -> b -> a" in msg


def test_source_must_reach_destination():
    msg = errors_of({"version": 1, "nodes": [src(), filt("f", "filter.live"), dest()],
                     "edges": [{"from": "s", "to": "f"}]})
    assert "source 's' does not reach any destination" in msg


def test_edge_direction_rules():
    msg = errors_of({"version": 1, "nodes": [src(), src("s2"), dest()],
                     "edges": [{"from": "s", "to": "s2"}, {"from": "d", "to": "s"}]})
    assert "source 's2' cannot have incoming" in msg and "destination 'd' cannot have outgoing" in msg


def test_channel_and_playlist_ids():
    assert "'s'" in errors_of({"version": 1, "nodes": [src(ch="UCshort"), dest()], "edges": [{"from": "s", "to": "d"}]})
    assert "'d'" in errors_of({"version": 1, "nodes": [src(), dest(pl="")], "edges": [{"from": "s", "to": "d"}]})
    g = graph([src(), dest(pl="abcdefghijkl")], [{"from": "s", "to": "d"}])
    assert any("not PL-prefixed" in w for w in g.warnings)


def test_invalid_regex_is_validation_error():
    msg = errors_of({"version": 1, "nodes": [src(), filt("f", "filter.regex", pattern="(unclosed"), dest()],
                     "edges": [{"from": "s", "to": "f"}, {"from": "f", "to": "d"}]})
    assert "'f'" in msg and "invalid regex" in msg


def test_param_type_errors():
    assert "'f'" in errors_of({"version": 1, "nodes": [src(), filt("f", "filter.shorts", mode="maybe"), dest()],
                               "edges": [{"from": "s", "to": "f"}, {"from": "f", "to": "d"}]})
    assert "'f'" in errors_of({"version": 1, "nodes": [src(), filt("f", "filter.keyword"), dest()],
                               "edges": [{"from": "s", "to": "f"}, {"from": "f", "to": "d"}]})


def test_bad_multi_match():
    assert "multi_match" in errors_of({"version": 1, "settings": {"multi_match": "some"},
                                       "nodes": [src(), dest()], "edges": [{"from": "s", "to": "d"}]})


# ---- filters ----

def test_keyword_include_exclude_whole_word_case():
    g = chain("filter.keyword", include=["recipe"], exclude=["sponsored"])
    assert passes(g, vid(title="Great RECIPE"))
    assert not passes(g, vid(title="Great recipes"))  # whole word
    assert not passes(g, vid(title="recipe Sponsored"))
    assert not passes(g, vid(title="nothing"))
    sub = chain("filter.keyword", include=["recipe"], whole_word=False)
    assert passes(sub, vid(title="recipes"))


def test_keyword_fields_and_exclude_only():
    g = chain("filter.keyword", include=["vegan"], fields=["tags"])
    assert passes(g, vid(title="x", tags=("Vegan",)))
    assert not passes(g, vid(title="vegan", tags=()))
    ex = chain("filter.keyword", exclude=["spoiler"], fields=["description"])
    assert passes(ex, vid(description="fine"))
    assert not passes(ex, vid(description="a Spoiler!"))


def test_regex_filter_and_truncation():
    g = chain("filter.regex", pattern=r"^ep\s*\d+")
    assert passes(g, vid(title="Ep 12 - x"))
    assert not passes(g, vid(title="Intro ep 12"))
    late = chain("filter.regex", pattern="needle", fields=["description"])
    assert not passes(late, vid(description="a" * 5000 + "needle"))
    neg = chain("filter.regex", pattern="trailer", negate=True)
    assert not passes(neg, vid(title="Trailer"))
    assert passes(neg, vid(title="Episode"))


def test_duration_filter():
    g = chain("filter.duration", min_seconds=60, max_seconds=900)
    assert passes(g, vid(duration_seconds=600))
    assert not passes(g, vid(duration_seconds=30))
    assert not passes(g, vid(duration_seconds=1000))


def test_shorts_filter():
    assert not passes(chain("filter.shorts", mode="exclude"), vid(is_short=True))
    assert passes(chain("filter.shorts", mode="exclude"), vid())
    assert passes(chain("filter.shorts", mode="only"), vid(is_short=True))
    assert not passes(chain("filter.shorts", mode="only"), vid())


def test_age_filter_uses_injected_now():
    g = chain("filter.age", max_days=3)
    assert passes(g, vid(published_at=NOW - timedelta(days=2)))
    assert not passes(g, vid(published_at=NOW - timedelta(days=4)))
    assert not execute(g, [vid(published_at=NOW - timedelta(days=2))], NOW + timedelta(days=5)).filings


def test_live_filter():
    g = chain("filter.live")
    assert passes(g, vid())
    assert not passes(g, vid(live_broadcast_content="live"))
    assert not passes(g, vid(live_broadcast_content="upcoming"))


# ---- executor ----

def two_dest_graph(**settings):
    return graph([src(), filt("f", "filter.keyword", include=["recipe"]), dest("d2", PL2), dest("d1", PL1)],
                 [{"from": "s", "to": "d2"}, {"from": "s", "to": "f"}, {"from": "f", "to": "d1"}], **settings)


def test_multi_match_all_and_first():
    v = vid()
    assert sorted(execute(two_dest_graph(), [v], NOW).filings) == [("v1", PL1), ("v1", PL2)]
    # sorted edge targets from s are d2, f: first match is d2
    assert execute(two_dest_graph(multi_match="first"), [v], NOW).filings == [("v1", PL2)]


def test_duplicate_destination_deduped():
    g = graph([src(), filt("a", "filter.live"), filt("b", "filter.live"), dest()],
              [{"from": "s", "to": "a"}, {"from": "s", "to": "b"}, {"from": "a", "to": "d"}, {"from": "b", "to": "d"}])
    assert execute(g, [vid()], NOW).filings == [("v1", PL1)]


def test_two_destinations_same_playlist_deduped():
    g = graph([src(), dest("d1"), dest("d2")], [{"from": "s", "to": "d1"}, {"from": "s", "to": "d2"}])
    assert execute(g, [vid()], NOW).filings == [("v1", PL1)]


def test_sources_route_by_channel():
    g = graph([src("sa", CH_A), src("sb", CH_B), dest("da", PL1), dest("db", PL2)],
              [{"from": "sa", "to": "da"}, {"from": "sb", "to": "db"}])
    r = execute(g, [vid("v1", CH_A), vid("v2", CH_B), vid("v3", "UC" + "z" * 22)], NOW)
    assert r.filings == [("v1", PL1), ("v2", PL2)]
    assert r.traces["v3"] == []


def test_group_and_subscriptions_sources():
    g = graph([{"id": "g", "type": "source.channel_group", "params": {"channel_ids": [CH_A, CH_B]}},
               {"id": "subs", "type": "source.subscriptions"}, dest()],
              [{"from": "g", "to": "d"}, {"from": "subs", "to": "d"}])
    assert execute(g, [vid(channel=CH_B)], NOW).filings == [("v1", PL1)]
    other = "UC" + "q" * 22
    assert execute(g, [vid(channel=other)], NOW, subscriptions={other}).filings == [("v1", PL1)]
    assert not execute(g, [vid(channel=other)], NOW, subscriptions={CH_A}).filings


def test_trace_and_counts():
    g = graph([src(), filt("kw", "filter.keyword", include=["recipe"]), dest()],
              [{"from": "s", "to": "kw"}, {"from": "kw", "to": "d"}])
    r = execute(g, [vid("hit"), vid("miss", title="other")], NOW)
    assert [(e.node_id, e.outcome) for e in r.traces["hit"]] == [("s", "pass"), ("kw", "pass"), ("d", "file")]
    assert [(e.node_id, e.outcome) for e in r.traces["miss"]] == [("s", "pass"), ("kw", "drop")]
    assert "include" in r.traces["miss"][1].reason
    assert r.node_counts["kw"]["pass"] == 1 and r.node_counts["kw"]["drop"] == 1
    assert r.node_counts["d"]["file"] == 1


def test_required_channels():
    g = graph([src(), {"id": "g", "type": "source.channel_group", "params": {"channel_ids": [CH_B]}}, dest()],
              [{"from": "s", "to": "d"}, {"from": "g", "to": "d"}])

    def boom():
        raise AssertionError("resolver should not be called")

    assert required_channels(g, boom) == {CH_A, CH_B}
    g2 = graph([{"id": "subs", "type": "source.subscriptions"}, dest()], [{"from": "subs", "to": "d"}])
    assert required_channels(g2, lambda: ["UC" + "s" * 22]) == {"UC" + "s" * 22}


def test_reserved_types_validate_but_raise():
    for typ in ("filter.llm", "router.llm"):
        g = graph([src(), filt("brain", typ, prompt="is it cooking?"), dest()],
                  [{"from": "s", "to": "brain"}, {"from": "brain", "to": "d"}])
        with pytest.raises(NotImplementedInThisVersion, match="brain"):
            execute(g, [], NOW)
    assert "'brain'" in errors_of({"version": 1, "nodes": [src(), filt("brain", "filter.llm"), dest()],
                                   "edges": [{"from": "s", "to": "brain"}, {"from": "brain", "to": "d"}]})
