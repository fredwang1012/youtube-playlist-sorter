"""End to end on the example graph with fakes: dry run, run, second run, then a crash and a re-run."""
from datetime import datetime, timedelta, timezone

import pytest

from app import cli, run as runner
from app.config import ROOT, settings
from tests.test_detect import feed_xml, video_item
from tests.test_run import Crash, FakeYT

CH1, CH2 = "UCexample000000000000001", "UCexample000000000000002"
PL = "PLexampleExamplePlaylist01"
TITLES = {"r1": "Easy sourdough recipe", "r2": "Baking day: recipe for rolls", "x1": "My vlog", "s1": "Sponsored recipe", "r3": "Weekend baking"}


class ExampleYT(FakeYT):
    def videos(self, ids):
        self._read("videos")
        published = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = []
        for v in ids:
            item = video_item(v, ch=CH1)
            item["snippet"].update(title=TITLES[v], tags=[], publishedAt=published)
            items.append(item)
        return items


@pytest.fixture
def setup(monkeypatch):
    yt = ExampleYT(owned=[PL])
    monkeypatch.setattr(cli, "_client", lambda: (yt, yt.meter))
    feeds = {CH1: ["r1", "r2", "x1", "s1"], CH2: []}
    monkeypatch.setattr(runner, "fetch_url", lambda url: feed_xml(*next(
        (c, ids) for c, ids in feeds.items() if c in url)))
    yt.feeds = feeds
    return yt


def run_cli(*extra):
    return cli.main(["run", "--graph", str(ROOT / "graphs" / "example.yaml"), "--backfill", "5", *extra])


def test_example_graph_end_to_end(setup, capsys):
    yt = setup
    state_db = settings.data_dir / "state.db"

    assert run_cli("--dry-run") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and f"Title {PL}: 2" in out
    assert yt.inserts == [] and not state_db.exists()

    assert run_cli() == 0
    out = capsys.readouterr().out
    assert sorted(yt.inserts) == [(PL, "r1"), (PL, "r2")]  # x1 has no recipe keyword, s1 is excluded
    assert f"Title {PL}: 2" in out

    assert run_cli() == 0
    capsys.readouterr()
    assert len(yt.inserts) == 2  # no duplicates

    # a new upload is accepted by YouTube, then the process dies before it is recorded
    yt.accept_then_raise[(PL, "r3")] = [Crash()]
    yt.feeds[CH1].append("r3")
    with pytest.raises(Crash):
        run_cli()
    assert yt.inserts.count((PL, "r3")) == 1
    assert run_cli() == 0
    assert yt.inserts.count((PL, "r3")) == 1  # the re-run verifies instead of inserting again
    assert len(yt.inserts) == 3
    capsys.readouterr()
