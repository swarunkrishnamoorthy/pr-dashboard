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
import time
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

# Concurrent GraphQL enrich calls (review + checks, one per PR).
ENRICH_WORKERS = 20

# How often the background thread rebuilds the cached PR snapshot.
REFRESH_SECONDS = 60


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
    """Normalize one statusCheckRollup entry to pass|pending|cancelled|fail."""
    status = item.get("status")        # CheckRun uses status + conclusion
    if status is not None:
        if status != "COMPLETED":
            return "pending"
        conclusion = item.get("conclusion")
        if conclusion == "CANCELLED":
            return "cancelled"
        return "pass" if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") else "fail"
    state = item.get("state")          # StatusContext uses state
    if state == "SUCCESS":
        return "pass"
    if state in ("PENDING", "EXPECTED"):
        return "pending"
    return "fail"


# GraphQL: fetch review decision, merge queue state, branch refs, push/comment
# activity, and per-check isRequired in one call.
# isRequired(pullRequestNumber:) reflects branch-protection required status — the
# only way to tell a merge-blocking failure from a failing optional check.
ENRICH_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "author{login} baseRefName headRefName reviewDecision isInMergeQueue "
    "commits(last:5){nodes{commit{committedDate}}} "
    "timelineItems(last:20,itemTypes:[PULL_REQUEST_COMMIT,HEAD_REF_FORCE_PUSHED_EVENT]){"
    "nodes{__typename "
    "... on PullRequestCommit{commit{committedDate}} "
    "... on HeadRefForcePushedEvent{createdAt}}} "
    "comments(last:50){nodes{author{login __typename}createdAt}} "
    "reviews(last:50){nodes{author{login __typename}state submittedAt bodyText}} "
    "reviewThreads(last:50){nodes{comments(last:20){nodes{author{login __typename}createdAt}}}} "
    "statusCheckRollup{contexts(first:100){nodes{__typename "
    "... on CheckRun{status conclusion isRequired(pullRequestNumber:$number)} "
    "... on StatusContext{state isRequired(pullRequestNumber:$number)}}}}}}}"
)


def _latest_push_at(pull):
    """Best-effort timestamp of the latest commit or force-push on the PR."""
    stamps = []
    for node in ((pull.get("commits") or {}).get("nodes") or []):
        dt = ((node.get("commit") or {}).get("committedDate")) or ""
        if dt:
            stamps.append(dt)
    for node in ((pull.get("timelineItems") or {}).get("nodes") or []):
        if node.get("__typename") == "HeadRefForcePushedEvent":
            dt = node.get("createdAt") or ""
        else:
            dt = ((node.get("commit") or {}).get("committedDate")) or ""
        if dt:
            stamps.append(dt)
    return max(stamps) if stamps else ""


def _is_actionable_author(author, pr_author):
    if not author:
        return False
    if author.get("__typename") == "Bot":
        return False
    login = author.get("login") or ""
    if not login or login == pr_author:
        return False
    if login.endswith("[bot]"):
        return False
    return True


def _builderai_flagged_issues(body):
    """BuilderAI comments on every push; only treat it as review comments when
    it actually flagged issues rather than posting an all-clear summary."""
    text = (body or "").lower()
    if "no major issues found" in text:
        return False
    return "major issue" in text or "changes requested" in text


def _has_comments_since_push(pull):
    """True when a human reviewer or an issue-flagging BuilderAI review
    commented after the latest commit/force-push on the PR."""
    latest_push = _latest_push_at(pull)
    if not latest_push:
        return False
    pr_author = ((pull.get("author") or {}).get("login")) or ""

    for c in ((pull.get("comments") or {}).get("nodes") or []):
        author = c.get("author")
        login = (author or {}).get("login") or ""
        if login == "BuilderAI":
            continue
        if _is_actionable_author(author, pr_author) and (c.get("createdAt") or "") > latest_push:
            return True

    for t in ((pull.get("reviewThreads") or {}).get("nodes") or []):
        for c in ((t.get("comments") or {}).get("nodes") or []):
            author = c.get("author")
            login = (author or {}).get("login") or ""
            # BuilderAI's inline comments are paired with a BuilderAI review,
            # so skip them here and inspect the review summary below.
            if login == "BuilderAI":
                continue
            if _is_actionable_author(author, pr_author) and (c.get("createdAt") or "") > latest_push:
                return True

    for r in ((pull.get("reviews") or {}).get("nodes") or []):
        author = r.get("author")
        submitted = r.get("submittedAt") or ""
        if not _is_actionable_author(author, pr_author) or submitted <= latest_push:
            continue
        login = (author or {}).get("login") or ""
        if login == "BuilderAI":
            if _builderai_flagged_issues(r.get("bodyText")):
                return True
            continue
        # An approval with no written review body isn't a comment to address.
        if (r.get("state") == "APPROVED") and not (r.get("bodyText") or "").strip():
            continue
        return True

    return False


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
    if pull.get("isInMergeQueue") and rd == "APPROVED":
        pr["review"] = {"label": "Merging", "cls": "merging"}
    elif rd == "APPROVED":
        pr["review"] = {"label": "approved", "cls": "good"}
    elif rd == "CHANGES_REQUESTED":
        pr["review"] = {"label": "changes requested", "cls": "bad"}
    elif _has_comments_since_push(pull):
        pr["review"] = {"label": "review comments", "cls": "warn"}
    else:
        pr["review"] = {
            "REVIEW_REQUIRED": {"label": "review pending", "cls": "warn"},
        }.get(rd, {"label": "no review required", "cls": "none"})

    nodes = (((pull.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes")) or []
    if not nodes:
        pr["checks"] = {"label": "no checks", "cls": "none"}
        return pr

    req_fail = req_pending = req_cancelled = False
    opt_fail = opt_pending = opt_cancelled = False
    for n in nodes:
        cls = _check_class(n)
        if n.get("isRequired"):
            req_fail = req_fail or cls == "fail"
            req_pending = req_pending or cls == "pending"
            req_cancelled = req_cancelled or cls == "cancelled"
        else:
            opt_fail = opt_fail or cls == "fail"
            opt_pending = opt_pending or cls == "pending"
            opt_cancelled = opt_cancelled or cls == "cancelled"

    if req_fail:
        pr["checks"] = {"label": "required checks failing", "cls": "bad"}
    elif req_cancelled:
        pr["checks"] = {"label": "required checks cancelled", "cls": "warn"}
    elif req_pending:
        pr["checks"] = {"label": "required checks in progress", "cls": "warn"}
    elif opt_fail:
        pr["checks"] = {"label": "optional checks failing", "cls": "warn"}
    elif opt_cancelled:
        pr["checks"] = {"label": "some checks cancelled", "cls": "warn"}
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
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
            prs = list(pool.map(enrich, prs))
        compute_stacks(prs)
    counts = {"open": 0, "merged": 0, "closed": 0}
    for p in prs:
        counts[p.get("state", "closed")] = counts.get(p.get("state", "closed"), 0) + 1
    prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"prs": prs, "errors": errors, "counts": counts,
            "windowDays": WINDOW_DAYS, "fetchedAt": fetched_at}


# The snapshot is rebuilt on a background timer and served from here, so a
# client poll is a cheap cache read rather than a ~14s live `gh` fetch.
_cache = {"data": None}
_cache_lock = threading.Lock()


def refresh_cache():
    """Rebuild the PR snapshot and store it. Returns the fresh snapshot."""
    data = fetch_all()
    with _cache_lock:
        _cache["data"] = data
    return data


def cached_snapshot():
    """Latest snapshot, computing one synchronously if the cache is cold."""
    with _cache_lock:
        data = _cache["data"]
    return data if data is not None else refresh_cache()


def background_refresher():
    """Rebuild the snapshot every REFRESH_SECONDS. First build is lazy, driven
    by the initial request, so we sleep before the first rebuild here."""
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            refresh_cache()
        except Exception:  # noqa: BLE001 -- a failed refresh must not kill the loop
            pass


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
                payload = json.dumps(cached_snapshot())
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
    threading.Thread(target=background_refresher, daemon=True).start()
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
