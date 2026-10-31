# youtube-playlist-sorter

Draw a flow chart of channels, filters and playlists, and a daily job runs it: new videos from the channels I care
about get filtered by what they're actually about and land in the right YouTube playlist automatically.

## How it works

| Node | What it does | Examples |
| --- | --- | --- |
| Source | Emits new videos | One channel, all subscriptions, a channel group |
| Filter | Passes or drops videos | Keyword include/exclude, regex, duration, skip Shorts, age, skip live streams |
| Destination | Adds the video to a playlist | Topic playlists |

The graph is the config, a YAML file. Every run can be previewed with a dry run before anything is written to
YouTube. A later version adds a visual editor and LLM-based filtering and routing (see Roadmap).

## Setup

Prerequisites: Windows, Python 3.12 with [uv](https://docs.astral.sh/uv/), and the Google account that owns the
playlists.

```bash
py -3.12 -m uv sync
```

### Google Cloud project

YouTube access needs your own OAuth client. These steps follow the current Google Auth Platform console
(console.cloud.google.com, "Google Auth Platform" in the left menu).

1. **Create a new, dedicated project.** Do not reuse another app's project. The OAuth consent screen (name, scopes,
   publishing status) is shared by every client in a project, so changing it here would change that app too.
2. **Enable the API.** APIs & Services, Library, search "YouTube Data API v3", Enable.
3. **Get started.** Google Auth Platform, Get started. Fill in the app name and a support email. Choose **External**
   as the audience, add a contact email, and accept the policy.
4. **Branding.** Set the app homepage link, a privacy policy link, and add the domain of those links under
   authorized domains. The privacy policy can be any public page you control, such as a GitHub gist saying the app
   only reads your channel and playlists and stores nothing outside your machine. Do not upload a logo: a logo
   triggers a verification review.
5. **Data Access.** Add or remove scopes, add `https://www.googleapis.com/auth/youtube.force-ssl`, and save.
6. **Audience.** Under Publishing status choose **Publish app** and confirm. The "requires verification" banner can be
   ignored for a tool only you use. An unverified app is capped at 100 users and shows an "unverified app" warning
   at sign-in, which is expected.
7. **Clients.** Create client, application type **Desktop app**, Create, then **Download JSON**. Save the file as
   `~/.config/youtube-playlist-sorter/client_secret.json` (on Windows,
   `C:\Users\<you>\.config\youtube-playlist-sorter\client_secret.json`). It stays outside the repo. Set
   `CLIENT_SECRET_PATH` in `.env` to keep it somewhere else.
8. **Sign in.**

   ```bash
   py -3.12 -m uv run python -m app.cli auth
   ```

   A browser tab opens. On the warning page choose **Advanced**, then **Go to (app name) (unsafe)**, and allow
   access. `whoami` then prints your channel name.

Do publish the app (step 6). An app left in **Testing** has refresh tokens that expire after 7 days, so the daily
job would stop every week until you ran `auth` again.

Other settings come from the environment or a `.env` file at the repo root: `DAILY_QUOTA_BUDGET` (default 8000),
`ALLOW_ANY_PLAYLIST` (default false), `DATA_DIR` and `CLIENT_SECRET_PATH`.

## Commands

```bash
py -3.12 -m uv run python -m app.cli whoami
py -3.12 -m uv run python -m app.cli playlists        # your playlist ids and titles
py -3.12 -m uv run python -m app.cli channels         # channel ids of your subscriptions
py -3.12 -m uv run python -m app.cli run --graph graphs\mine.yaml [--dry-run] [--backfill N]
py -3.12 -m uv run python -m app.cli preview --graph graphs\mine.yaml --node <node id>
```

## Writing a graph

A graph is a YAML or JSON file with `nodes` and `edges`. Copy [graphs/example.yaml](graphs/example.yaml) to
`graphs/mine.yaml` (git ignores that name) and edit it. Every node type:

```yaml
version: 1
settings:
  multi_match: all          # "all": file into every destination reached; "first": stop at the first one
nodes:
  - id: cooking_channels
    type: source.channel_group      # also: source.channel (channel_id), source.subscriptions (no params)
    params:
      channel_ids: [UCxxxxxxxxxxxxxxxxxxxxxx, UCyyyyyyyyyyyyyyyyyyyyyy]
  - id: no_shorts
    type: filter.shorts             # mode: exclude | only
    params: {mode: exclude}
  - id: no_live
    type: filter.live               # drops live streams and premieres that have not aired
  - id: long_enough
    type: filter.duration           # min_seconds and/or max_seconds
    params: {min_seconds: 300}
  - id: recent
    type: filter.age                # drops videos published more than max_days ago
    params: {max_days: 7}
  - id: recipes
    type: filter.keyword            # whole word, case-insensitive; passes if any include term matches
    params:
      include: [recipe, baking]
      exclude: [sponsored]
      fields: [title, tags]         # title, description, tags
  - id: not_a_vlog
    type: filter.regex              # negate: true drops on a match
    params: {pattern: "vlog|q&a", fields: [title], negate: true}
  - id: cooking_playlist
    type: destination.playlist
    params: {playlist_id: PLxxxxxxxxxxxxxxxxxxxxxx, title: Cooking}
edges:
  - {from: cooking_channels, to: no_shorts}
  - {from: no_shorts, to: no_live}
  - {from: no_live, to: long_enough}
  - {from: long_enough, to: recent}
  - {from: recent, to: recipes}
  - {from: recipes, to: not_a_vlog}
  - {from: not_a_vlog, to: cooking_playlist}
```

A source emits videos, each filter passes or drops them, and a destination files whatever reaches it. A node can
have several outgoing edges, so one source can feed several branches. The loader rejects unknown node types, bad
parameters, cycles and unreachable destinations, naming the node. `filter.llm` and `router.llm` are reserved for a
later version and fail at run time today.

Find the ids with the tool: `channels` prints the `UC...` channel id of each subscription, and `playlists` prints the
`PL...` id of each playlist you own. A destination must be a playlist you own.

The first time a channel is seen, its current feed is marked as seen so the back catalogue is not filed. Pass
`--backfill 5` to take the newest five videos of each new channel instead.

## Dry run first

```bash
py -3.12 -m uv run python -m app.cli run --graph graphs\mine.yaml --dry-run
py -3.12 -m uv run python -m app.cli preview --graph graphs\mine.yaml --node recipes
```

`run --dry-run` works on a throwaway copy of the state and never inserts. It prints, per node, how many videos passed
and dropped with up to three example titles, the projected filings per playlist, and the quota a real run would
spend. `preview` lists the recent videos that pass one node, so a keyword list can be tuned before it is wired to a
playlist. Both still make read calls, which cost a few quota units.

## Quota

YouTube gives each project 10,000 units a day, reset at midnight Pacific time. The tool counts its own usage in SQLite
per Pacific day and stops inserting before the budget (default 8000), so a bad day cannot lock the project out.

| Call | Cost |
| --- | --- |
| RSS feed, per channel | free (not an API call) |
| `videos.list` | 1 unit per 50 videos |
| `playlists.list` (my playlists) | 1 unit |
| `subscriptions.list` | 1 unit per page of 50, cached for 24 hours |
| `channels.list` + `playlistItems.list` (uploads fallback, per channel) | 2 units |
| `playlistItems.list` (check after a crash) | 1 unit |
| `playlistItems.insert` | 50 units |

A worked day: 150 subscriptions, about 60 new uploads, 25 of them match a rule.

- my playlists: 1
- subscriptions, 3 pages: 3
- `videos.list`, 60 videos in 2 batches: 2
- 25 inserts at 50 each: 1250
- Total: about 1256 units, well under 8000.

At the default budget the cap is 160 inserts a day. Filings over the cap wait in a queue and go in the next day. The
run summary shows the units used this run and today.

## Safety

- **Insert-only.** The only write the tool makes is `playlistItems.insert`. It never deletes or edits videos,
  playlists or subscriptions, and never calls `search.list`.
- **Only playlists you named.** It writes only to playlists that are destinations in your graph and owned by the
  signed-in account. A foreign playlist id stops the run before any work. `allow_any_playlist` (default false)
  overrides that.
- **No duplicates, even after a crash.** Each insert is recorded in SQLite before it is sent. If the process dies
  mid-insert, the next run sees the open record, asks YouTube whether the video is already in the playlist (1 unit),
  and inserts only if it is not. A recorded filing is never inserted twice.
- **Residual risk.** The HTTP library may resend a request once if the connection drops mid-call. In that rare case
  YouTube could add the same video twice. It is the one gap the duplicate check cannot close.
- **Full playlists.** A playlist that hits YouTube's 5000-item limit is skipped for 24 hours and its filings stay
  queued.
- **Token storage.** The refresh token lives in the Windows Credential Manager through `keyring`, never in a file,
  the repo or the logs.
- **Revoking access.** Remove the app at https://myaccount.google.com/permissions. The next run then tells you to run
  `auth` again.

## Daily schedule (Windows Task Scheduler)

`scripts/run_daily.cmd` changes to the repo, runs the tool and appends its output to `data\run.log`. The log holds run
summaries and error messages, no tokens. It runs `graphs\mine.yaml`, or the graph you pass as its first argument.

Create the task (this one runs at 06:30 every day; change the path and time):

```bat
schtasks /Create /TN "YouTube Playlist Sorter" /SC DAILY /ST 06:30 /F /TR "\"C:\dev\UofT\Projects\youtube-playlist-sorter\scripts\run_daily.cmd\""
```

The wrapper sets the working directory, so the task needs no "start in" folder. The task runs as you, only while you
are signed in, which the keychain needs. If the computer is asleep at 06:30, open Task Scheduler, the task's Settings
tab, and tick "Run task as soon as possible after a scheduled start is missed".

```bat
schtasks /Run /TN "YouTube Playlist Sorter"
schtasks /Query /TN "YouTube Playlist Sorter" /V /FO LIST
schtasks /Delete /TN "YouTube Playlist Sorter" /F
```

Exit codes: 0 success, 1 error or invalid graph, 2 Google sign-in expired or revoked (run `auth`), 3 another run is
already in progress (real runs take a lock on `data/run.lock`; dry runs and `preview` do not), 4 network unavailable
(try again later; anything already filed stays recorded and queued filings are kept), 5 real runs are turned off on
this machine (`REAL_RUNS_DISABLED=true`, see below).

Read the log with `Get-Content data\run.log -Tail 40` in PowerShell. If it says Google sign-in expired or was
revoked, run `auth` again.

## Running on a server

The web UI and the scheduled runs can live on a small Linux VM, so they work with the laptop off. A systemd service
serves the page on `127.0.0.1`, a systemd timer runs the sort every 6 hours, `tailscale serve` publishes it as HTTPS
on the tailnet (never Funnel), and the secrets are encrypted with `systemd-creds`. The kit is in `deploy/`
(`setup.sh` on the VM, `push.sh` from Git Bash on the laptop, and the unit files). Setup, the security model, checks
and how to revoke everything: [docs/REMOTE.md](docs/REMOTE.md).

Once the VM runs real sorts, the laptop must not: the two keep separate `state.db` files, so a laptop run would file
the same videos again. Delete the scheduled task and add `REAL_RUNS_DISABLED=true` to the laptop's `.env`, which makes
`run` without `--dry-run` (and Check now in a local UI) refuse with exit code 5.

## Tests

```bash
py -3.12 -m uv run pytest -q
```

No test uses the network, the real keychain or the real API. A fake YouTube client and synthetic RSS feeds stand in,
and `tests/test_e2e.py` runs the example graph through a dry run, a run, a repeat run and a simulated crash.

## Limitations

- An RSS feed lists only a channel's latest 15 videos. When a full feed has no video the tool has seen before, or the
  feed fails to load, it reads the channel's uploads playlist instead (2 units), which covers a larger gap. A burst
  of uploads bigger than that gap between two runs can still be missed, so run the job at least daily.
- Watch Later is not accessible through the API.
- Filters see only what YouTube returns for a video: title, description, tags, duration, category. Nothing reads the
  transcript or the picture.
- One account, one machine. The state lives in `data/state.db`.

## Roadmap

- A visual editor on React Flow to draw the graph instead of writing YAML (Tier 3).
- An LLM filter and router node that sorts by what a video is about, with a Review playlist for low-confidence
  videos (Tier 4). The graph format already reserves `filter.llm` and `router.llm`.

## License

Copyright (c) 2026 Frederick Wang. All rights reserved. See [LICENSE](LICENSE).
