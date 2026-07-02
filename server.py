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


def enrich(pr):
    """Add review + checks status to a PR via `gh pr view`. gh infers host from the URL."""
    pr["review"] = {"label": "—", "cls": "none"}
    pr["checks"] = {"label": "—", "cls": "none"}
    cmd = ["gh", "pr", "view", pr["url"], "--json", "reviewDecision,statusCheckRollup"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return pr
        data = json.loads(out.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return pr

    rd = data.get("reviewDecision") or ""
    pr["review"] = {
        "APPROVED": {"label": "approved", "cls": "good"},
        "CHANGES_REQUESTED": {"label": "changes requested", "cls": "bad"},
        "REVIEW_REQUIRED": {"label": "review pending", "cls": "warn"},
    }.get(rd, {"label": "no review required", "cls": "none"})

    rollup = data.get("statusCheckRollup") or []
    if not rollup:
        pr["checks"] = {"label": "no required actions", "cls": "none"}
    else:
        classes = [_check_class(i) for i in rollup]
        if "fail" in classes:
            pr["checks"] = {"label": "required actions failing", "cls": "bad"}
        elif "pending" in classes:
            pr["checks"] = {"label": "actions in progress", "cls": "warn"}
        else:
            pr["checks"] = {"label": "all required actions passing", "cls": "good"}
    return pr


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
