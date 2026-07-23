#!/usr/bin/env python3
"""Local dashboard of your authored GitHub PRs (github.com + github.rbx.com).

Run:  python3 server.py            # serves on http://localhost:8787
      python3 server.py --port N   # custom port
      python3 server.py --no-open  # don't auto-open the browser

Data is fetched live via the `gh` CLI (must be authenticated on each host).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

# Hosts to query. label is shown in the UI; host is the GH_HOST value.
HOSTS = [
    {"host": "github.rbx.com", "label": "rbx"},
    {"host": "github.com", "label": "github.com"},
]

JSON_FIELDS = "number,title,state,url,repository,createdAt,updatedAt,closedAt,isDraft,labels"

# Only show PRs updated within this many days.
WINDOW_DAYS = 14


def fetch_host(entry):
    """Fetch authored PRs for one host via `gh search prs`. Returns (prs, error)."""
    cutoff = datetime.date.today() - datetime.timedelta(days=WINDOW_DAYS)
    env = dict(os.environ, GH_HOST=entry["host"])
    cmd = [
        "gh", "search", "prs",
        "--author=@me",
        "--updated", f">={cutoff.isoformat()}",
        "--limit", "1000",
        "--json", JSON_FIELDS,
    ]
    try:
        out = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        return [], "`gh` CLI not found on PATH"
    except subprocess.TimeoutExpired:
        return [], "timed out fetching PRs"
    if out.returncode != 0:
        return [], (out.stderr or out.stdout or "unknown error").strip()
    try:
        rows = json.loads(out.stdout or "[]")
    except json.JSONDecodeError as e:
        return [], f"bad JSON from gh: {e}"
    for r in rows:
        r["host"] = entry["host"]
        r["hostLabel"] = entry["label"]
    return rows, None


def _check_class(item):
    """Normalize one statusCheckRollup entry to pass|pending|fail."""
    status = item.get("status")        # CheckRun uses status + conclusion
    if status is not None:
        if status != "COMPLETED":
            return "pending"
        return "pass" if item.get("conclusion") in ("SUCCESS", "NEUTRAL", "SKIPPED") else "fail"
    state = item.get("state")          # StatusContext uses state
    if state == "SUCCESS":
        return "pass"
    if state in ("PENDING", "EXPECTED"):
        return "pending"
    return "fail"


# GraphQL: fetch review decision, branch refs, and per-check isRequired in one call.
# isRequired(pullRequestNumber:) reflects branch-protection required status — the
# only way to tell a merge-blocking failure from a failing optional check.
ENRICH_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "baseRefName headRefName reviewDecision "
    "statusCheckRollup{contexts(first:100){nodes{__typename "
    "... on CheckRun{status conclusion isRequired(pullRequestNumber:$number)} "
    "... on StatusContext{state isRequired(pullRequestNumber:$number)}}}}}}}"
)


def enrich(pr):
    """Add review + checks status to a PR.

    Uses GraphQL (not `gh pr view`) so we can read each check's `isRequired`
    flag: a failing *optional* check must not be reported as required/blocking.
    """
    pr["review"] = {"label": "—", "cls": "none"}
    pr["checks"] = {"label": "—", "cls": "none"}
    pr["baseRefName"] = ""   # branch this PR targets (for stack detection)
    pr["headRefName"] = ""   # this PR's own branch

    parts = urlparse(pr["url"])            # .../<owner>/<repo>/pull/<number>
    seg = parts.path.strip("/").split("/")
    if len(seg) < 4 or not seg[-1].isdigit():
        return pr
    owner, name, number = seg[0], seg[1], seg[-1]

    cmd = ["gh", "api", "graphql", "--hostname", parts.netloc,
           "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}",
           "-f", f"query={ENRICH_QUERY}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return pr
        data = json.loads(out.stdout or "{}")
        pull = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return pr
    if not pull:
        return pr

    pr["baseRefName"] = pull.get("baseRefName") or ""
    pr["headRefName"] = pull.get("headRefName") or ""

    rd = pull.get("reviewDecision") or ""
    pr["review"] = {
        "APPROVED": {"label": "approved", "cls": "good"},
        "CHANGES_REQUESTED": {"label": "changes requested", "cls": "bad"},
        "REVIEW_REQUIRED": {"label": "review pending", "cls": "warn"},
    }.get(rd, {"label": "no review required", "cls": "none"})

    nodes = (((pull.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes")) or []
    if not nodes:
        pr["checks"] = {"label": "no checks", "cls": "none"}
        return pr

    req_fail = req_pending = opt_fail = opt_pending = False
    for n in nodes:
        cls = _check_class(n)
        if n.get("isRequired"):
            req_fail = req_fail or cls == "fail"
            req_pending = req_pending or cls == "pending"
        else:
            opt_fail = opt_fail or cls == "fail"
            opt_pending = opt_pending or cls == "pending"

    if req_fail:
        pr["checks"] = {"label": "required checks failing", "cls": "bad"}
    elif req_pending:
        pr["checks"] = {"label": "required checks in progress", "cls": "warn"}
    elif opt_fail:
        pr["checks"] = {"label": "optional checks failing", "cls": "warn"}
    elif opt_pending:
        pr["checks"] = {"label": "checks in progress", "cls": "warn"}
    else:
        pr["checks"] = {"label": "all checks passing", "cls": "good"}
    return pr


def compute_stacks(prs):
    """Detect PR stacks: chains where one open PR's base branch is another open
    PR's head branch (same host + repo). A PR targeting master/main isn't a stack
    link because no PR has master as its head. Annotates each stacked PR in place
    with stackId, stackDepth (0 = base-most), stackSize, and stackBaseNumber.
    """
    # Only open PRs — the client shows states in separate tabs, and stacks are
    # about active work. Detecting across states would render half-empty stacks.
    open_prs = [p for p in prs if p.get("state") == "open"]

    def key(p, ref):
        return (p.get("host"), p["repository"]["nameWithOwner"], ref)

    by_head = {key(p, p["headRefName"]): i for i, p in enumerate(open_prs) if p.get("headRefName")}

    parent = [None] * len(open_prs)          # parent[i] = index of the PR that i is stacked on
    for i, p in enumerate(open_prs):
        base = p.get("baseRefName")
        if base:
            j = by_head.get(key(p, base))
            if j is not None and j != i:
                parent[i] = j

    # union-find to group connected chains/trees into stacks
    uf = list(range(len(open_prs)))
    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x
    for i, par in enumerate(parent):
        if par is not None:
            uf[find(i)] = find(par)

    comps = {}
    for i in range(len(open_prs)):
        comps.setdefault(find(i), []).append(i)

    stack_id = 0
    for members in comps.values():
        if len(members) < 2:
            continue
        stack_id += 1
        for i in members:
            depth, cur, seen = 0, i, set()
            while parent[cur] is not None and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
                depth += 1
            p = open_prs[i]
            p["stackId"] = stack_id
            p["stackDepth"] = depth
            p["stackSize"] = len(members)
            if parent[i] is not None:
                p["stackBaseNumber"] = open_prs[parent[i]]["number"]


def fetch_all():
    prs, errors = [], []
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        for entry, (rows, err) in zip(HOSTS, pool.map(fetch_host, HOSTS)):
            if err:
                errors.append({"host": entry["host"], "error": err})
            prs.extend(rows)
    if prs:  # enrich with review + checks status, concurrently
        with ThreadPoolExecutor(max_workers=8) as pool:
            prs = list(pool.map(enrich, prs))
        compute_stacks(prs)
    counts = {"open": 0, "merged": 0, "closed": 0}
    for p in prs:
        counts[p.get("state", "closed")] = counts.get(p.get("state", "closed"), 0) + 1
    prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
    return {"prs": prs, "errors": errors, "counts": counts, "windowDays": WINDOW_DAYS}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/prs":
            try:
                payload = json.dumps(fetch_all())
                self._send(200, payload, "application/json")
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}), "application/json")
            return
        # everything else -> the SPA
        try:
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(404, "index.html not found", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    url = f"http://localhost:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PR dashboard running at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    main()
