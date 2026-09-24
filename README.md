# PR Dashboard

A tiny, locally-run dashboard of **your authored GitHub pull requests**, across
both `github.com` and `github.rbx.com`. No build step, no dependencies — just
Python's standard library and the [`gh`](https://cli.github.com/) CLI.

![tabs: open / merged / closed, grouped by repo, with review + checks status]

## Features

- **Only your PRs** — uses `gh search prs --author=@me` on each host.
- **Three tabs** — Open / Merged / Closed, each with a live count.
- **Grouped by repo** — collapsible sections; expand-all / collapse-all.
- **Search** — live filter across title, repo, `#number`, and label.
- **Last 2 weeks** — only PRs updated within `WINDOW_DAYS` (default 14).
- **Per-PR status** — review decision (`draft` / `approved` / `changes requested`
  / `review pending`) and a colored checks badge (`all checks passing` /
  `required checks failing` / `required checks cancelled` / `checks in progress` /
  `optional checks failing`), fetched via GraphQL so required vs. optional
  checks are distinguished — a failing optional check isn't shown as blocking.
- **PR stacks** — stacked PRs (one PR branching off another open PR's head
  branch) are grouped and indented base → tip with their depth shown.
- **Auto-refresh** — the server rebuilds its PR snapshot in the background
  every 60s; the page polls the cached snapshot every minute and on tab focus,
  so refreshes are instant and don't hammer the GitHub API.
- **Both hosts** — combined view with a host filter; if one host's token is
  bad, the other still loads.

## Requirements

- Python 3.8+ (standard library only — no pip installs)
- `gh` CLI, authenticated on the host(s) you care about. The dashboard shows
  PRs authored by whoever `gh` is logged in as on each host (`--author=@me`),
  so there are no usernames or config to fill in — each teammate just uses
  their own `gh` login:
  ```sh
  gh auth login                      # github.com
  gh auth login -h github.rbx.com    # GitHub Enterprise (optional)
  ```

## Usage

```sh
python3 server.py                # serves http://localhost:8787 and opens a browser
python3 server.py --port 9000    # custom port
python3 server.py --no-open      # don't auto-open the browser
```

The page auto-refreshes every minute from a server-side cache that is rebuilt
every `REFRESH_SECONDS` (default 60); **↻ Refresh** re-reads the latest
snapshot immediately. The first page load after starting the server takes
longer (~15s) while the first snapshot is built.

## Configuration

Edit the constants at the top of [`server.py`](server.py):

- `HOSTS` — which GitHub hosts to query (and their UI labels).
- `WINDOW_DAYS` — how many days back to include (by last-updated).
- `REFRESH_SECONDS` — how often the background thread rebuilds the snapshot.
- `ENRICH_WORKERS` — concurrent per-PR GraphQL calls (review + checks status).

## How it works

`server.py` is a single-file stdlib HTTP server. A background thread rebuilds
a PR snapshot every `REFRESH_SECONDS` using `gh` (a `gh search prs` per host
plus one GraphQL call per PR for review decision, checks, and stack refs);
`GET /api/prs` serves that cached snapshot as JSON, and everything else serves
the single-page [`index.html`](index.html), which renders and filters
client-side. The server binds to 127.0.0.1 only, and no data leaves your
machine except the GitHub API calls `gh` already makes.
