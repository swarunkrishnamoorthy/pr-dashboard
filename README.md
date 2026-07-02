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
  / `review pending`) and a colored checks badge (`all required actions passing`
  / `required actions failing` / `actions in progress`), pulled live via
  `gh pr view`.
- **Both hosts** — combined view with a host filter; if one host's token is
  bad, the other still loads.

## Requirements

- Python 3.8+
- `gh` CLI, authenticated on the host(s) you care about:
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

Click **↻ Refresh** to re-fetch. Data is fetched live on each load.

## Configuration

Edit the constants at the top of [`server.py`](server.py):

- `HOSTS` — which GitHub hosts to query (and their UI labels).
- `WINDOW_DAYS` — how many days back to include (by last-updated).

## How it works

`server.py` is a single-file stdlib HTTP server. `GET /api/prs` shells out to
`gh` to fetch and enrich your PRs and returns JSON; everything else serves the
single-page [`index.html`](index.html), which renders and filters client-side.
No data leaves your machine except the GitHub API calls `gh` already makes.
