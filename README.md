# youtube-playlist-sorter

Draw a flow chart of channels, filters and playlists, and a daily job runs it: new videos from the channels I care
about get filtered by what they're actually about and land in the right YouTube playlist automatically.

## How it works

| Node | What it does | Examples |
| --- | --- | --- |
| Source | Emits new videos | One channel, all subscriptions, a channel group |
| Filter | Passes or drops videos | Keyword include/exclude, regex, duration, skip Shorts, "is this about X?" |
| Router | Sends each video down one branch | LLM classifier over several topics, with a confidence threshold |
| Destination | Adds the video to a playlist | Topic playlists, plus a "Review" playlist for low-confidence items |

The graph is the config, built by drag and drop. Every filter and router has a dry-run preview, so rules can be tuned
before anything is written to YouTube.

## Quota

YouTube gives 10,000 units/day per project. New uploads are detected from each channel's RSS feed (free) and the
uploads playlist, never `search.list`. Reads cost 1 unit; each playlist insert costs 50. Videos are deduped in SQLite
before writing, and the UI shows a quota meter.

## Status

Setup and research. Google Cloud OAuth and the quota budget are being finalized before the first build.

## License

Copyright (c) 2026 Frederick Wang. All rights reserved. See [LICENSE](LICENSE).
