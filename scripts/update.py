#!/usr/bin/env python3
"""Rebuild the dataset from GitHub.

    python scripts/update.py                       # pull requests, issues, projects, summaries
    python scripts/update.py --details             # also the code and the discussion of each PR
    python scripts/update.py --details --max-calls 900

Needs a token in GITHUB_TOKEN / GH_TOKEN or a logged-in gh CLI. Standard library only.

Without --details it uses the search API only. With --details it also calls the REST API once or
more per pull request: the files a merged PR changed (fixes/), and the reviews and comments on every
PR (discussion/). That is several thousand calls the first time, so --max-calls caps a run and the
next run carries on; a PR is fetched again only when GitHub reports it changed. The weekly workflow
runs it this way.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median

AUTHOR = "pcbeingused333"
START = date(2021, 1, 1)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FIXES = ROOT / "fixes"
DISCUSSION = ROOT / "discussion"
INDEX = DATA / "details_index.json"

# First matching rule wins; the order puts specific LLM-integration classes before generic ones.
CATEGORIES = [
    ("Retired model defaults", r"retir|deprecat|shut ?down|sunset|default(s|ing)? (the )?\w*\s*(model|to (gpt|claude|gemini))|replace (the )?(invalid|old|outdated)|current (claude|model)"),
    ("Streaming and tool calls", r"stream|tool[ _-]?call|tool_use|tool_choice|tool result|function[ _]call|parallel tool|\bchunk"),
    ("Token usage and cost", r"token|usage|cost|cache (read|write|creation)|pricing|billing"),
    ("RAG and vector stores", r"pinecone|faiss|qdrant|chroma|milvus|weaviate|lancedb|pgvector|vector|retriev|embedding|rerank|chunk(er|ing)|document (store|loader)|ingest"),
    ("Concurrency and async", r"async|await|event loop|concurren|thread|fork|max_workers|parallel|race|deadlock|lock\b"),
    ("Observability and tracing", r"span|trac(e|ing)|otel|opentelemetry|gen_ai|telemetry|metric|instrument"),
    ("Errors, retries and limits", r"retr(y|ies)|rate limit|429|timeout|backoff|exception|raise|crash|error|panic|fail"),
    ("Provider API compatibility", r"anthropic|openai|gemini|bedrock|mistral|vertex|azure|cohere|deepseek|ollama|responses api|finish reason|stop reason|refusal|reasoning|thinking|temperature|top_p|max_tokens"),
    ("Parsing and validation", r"pars|validat|schema|json|yaml|csv|format|escape|encod|decod|utf|bom|regex|serializ|coerc|cast"),
    ("Security", r"secur|auth|plaintext|credential|secret|xss|inject|sanitiz|ssrf|permission"),
    ("Documentation", r"^\s*docs?[\(:]|readme|example"),
]

# Bug patterns that recur across repositories, counted over merged pull requests.
PATTERNS = {
    "Streamed parallel tool calls merged when the provider omits an index": r"(without|no|omits?|carry no|sends no) (an )?index|same index|parallel tool call",
    "Sampling parameters sent to Claude models that reject them": r"(temperature|top_p|sampling).*(claude|opus|sonnet|reject)|(claude|opus|sonnet).*(temperature|top_p|sampling)",
    "Cache read and write tokens miscounted": r"cache (read|write|creation|token)|cache.*tokens",
    "Reasoning / thinking tokens missing from usage": r"(thinking|reasoning|thoughts?)[\w ]*tokens|thoughtsTokenCount",
    "Anthropic cumulative message_delta usage": r"message_delta|cumulative",
    "Unknown or unmapped finish / stop reasons": r"finish[ _]reason|stop[ _]?reason",
    "Defaults pointing at retired models": r"retir|shut ?down|deprecat",
    "Gemini responses with no content or blocked": r"(candidate|response) (without|has no|with no) content|safety filter|blocked",
    "Tool calls with empty or invalid JSON arguments": r"(blank|empty|invalid|not valid).*(argument|json)|arguments are not valid",
}

AI_DISCLOSURE = re.compile(r"claude|AI-assisted|generated with|copilot|codex|LLM-assisted", re.I)
TESTS_MENTIONED = re.compile(r"\btests?\b|regression|pytest|spec", re.I)
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__|testing)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|Tests?\.cs$|Test\.java$", re.I)


class RateLimited(Exception):
    pass


class Api:
    def __init__(self, max_calls: int | None = None) -> None:
        self.max_calls = max_calls
        self.calls = 0
        self.token = self._token()

    @staticmethod
    def _token() -> str | None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            return token
        if shutil.which("gh"):
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
            if out.returncode == 0:
                return out.stdout.strip()
        return None

    def get(self, path: str, params: dict[str, str | int] | None = None, budgeted: bool = False):
        if budgeted and self.max_calls is not None and self.calls >= self.max_calls:
            raise RateLimited("call budget for this run used up")
        url = f"https://api.github.com/{path}?{urllib.parse.urlencode(params or {})}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "merge-log-data"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(6):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                    if budgeted:
                        self.calls += 1
                    return json.load(resp)
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    return None
                remaining = err.headers.get("x-ratelimit-remaining")
                if err.code in (403, 429) and remaining == "0" and budgeted:
                    raise RateLimited("GitHub rate limit reached") from err
                if err.code in (403, 429, 502, 503) and attempt < 5:
                    time.sleep(min(60, 10 * (attempt + 1)))  # search API: 30 requests/minute with a token
                    continue
                raise
        raise RuntimeError(f"giving up on {url}")

    def pages(self, path: str, budgeted: bool = True, limit: int = 30) -> list:
        out = []
        for page in range(1, limit + 1):
            items = self.get(path, {"per_page": 100, "page": page}, budgeted=budgeted) or []
            out += items
            if len(items) < 100:
                break
        return out


def _windows(api: Api, kind: str) -> list[tuple[date, date]]:
    """Date windows of creation, each with fewer than 1,000 search results."""
    out, todo = [], [(START, date.today() + timedelta(days=1))]
    while todo:
        lo, hi = todo.pop()
        q = f"author:{AUTHOR} type:{kind} created:{lo.isoformat()}..{hi.isoformat()}"
        n = api.get("search/issues", {"q": q, "per_page": 1})["total_count"]
        if n == 0:
            continue
        if n < 1000 or lo == hi:
            out.append((lo, hi))
        else:
            mid = lo + (hi - lo) // 2
            todo += [(lo, mid), (mid + timedelta(days=1), hi)]
    return sorted(out)


def search_all(api: Api, kind: str) -> list[dict]:
    items: dict[str, dict] = {}
    for lo, hi in _windows(api, kind):
        q = f"author:{AUTHOR} type:{kind} created:{lo.isoformat()}..{hi.isoformat()}"
        for page in range(1, 11):
            batch = api.get("search/issues", {"q": q, "per_page": 100, "page": page})["items"]
            for it in batch:
                items[it["html_url"]] = it
            if len(batch) < 100:
                break
    return list(items.values())


def _repo_of(item: dict) -> str:
    return item["repository_url"].removeprefix("https://api.github.com/repos/")


def fetch_pull_requests(api: Api) -> list[dict]:
    prs = []
    for it in search_all(api, "pr"):
        pr = it.get("pull_request") or {}
        merged_at = pr.get("merged_at")
        body = it.get("body") or ""
        prs.append(
            {
                "repo": _repo_of(it),
                "number": it["number"],
                "title": it["title"],
                "url": it["html_url"],
                "state": "merged" if merged_at else it["state"],
                "created_at": it["created_at"],
                "updated_at": it["updated_at"],
                "merged_at": merged_at,
                "closed_at": it.get("closed_at"),
                "labels": [label["name"] for label in it.get("labels", [])],
                "comments": it.get("comments", 0),
                "ai_disclosed": bool(AI_DISCLOSURE.search(body)),
                "tests_mentioned": bool(TESTS_MENTIONED.search(body)),
            }
        )
    return sorted(prs, key=lambda p: p["created_at"], reverse=True)


def fetch_issues(api: Api) -> list[dict]:
    issues = []
    for it in search_all(api, "issue"):
        issues.append(
            {
                "repo": _repo_of(it),
                "number": it["number"],
                "title": it["title"],
                "url": it["html_url"],
                "state": it["state"],
                "state_reason": it.get("state_reason"),
                "created_at": it["created_at"],
                "closed_at": it.get("closed_at"),
                "labels": [label["name"] for label in it.get("labels", [])],
                "comments": it.get("comments", 0),
            }
        )
    return sorted(issues, key=lambda i: i["created_at"], reverse=True)


def fetch_projects(api: Api) -> list[dict]:
    items = api.get("search/repositories", {"q": f"user:{AUTHOR} fork:false", "per_page": 100})["items"]
    projects = [
        {
            "name": it["name"],
            "url": it["html_url"],
            "description": it["description"],
            "language": it["language"],
            "stars": it["stargazers_count"],
            "homepage": it["homepage"] or None,
            "created_at": it["created_at"],
            "pushed_at": it["pushed_at"],
        }
        for it in items
        if it["name"] != "my-first-repository" and not it.get("private")  # never list private repos
    ]
    return sorted(projects, key=lambda p: p["pushed_at"], reverse=True)


def fetch_repos(api: Api, names: list[str]) -> dict[str, dict]:
    repos: dict[str, dict] = {}
    batch: list[str] = []

    def flush() -> None:
        if not batch:
            return
        query = " ".join(f"repo:{n}" for n in batch)
        for it in api.get("search/repositories", {"q": query, "per_page": 100})["items"]:
            repos[it["full_name"].lower()] = {
                "stars": it["stargazers_count"],
                "language": it["language"],
                "description": it["description"],
                "owner_type": it["owner"]["type"],
            }
        batch.clear()

    for name in names:
        if len(" ".join(f"repo:{n}" for n in [*batch, name])) > 220:
            flush()
        batch.append(name)
    flush()
    return repos


def categorize(title: str) -> str:
    for name, rx in CATEGORIES:
        if re.search(rx, title, re.I):
            return name
    return "Application logic"


def _role(user: dict | None, association: str | None) -> str:
    """Who wrote a comment, without naming them: the author of the PR, a bot, or the project role."""
    if not user:
        return "unknown"
    if user.get("login") == AUTHOR:
        return "author"
    if user.get("type") == "Bot" or user.get("login", "").endswith("[bot]"):
        return "bot"
    return (association or "none").lower()


def _detail_path(base: Path, pr: dict) -> Path:
    owner, repo = pr["repo"].split("/", 1)
    return base / owner / repo / f"{pr['number']}.json"


def fetch_details(api: Api, prs: list[dict]) -> dict:
    """Code of merged PRs and the discussion of all PRs, incremental. Returns the updated index."""
    index = json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    # Merged PRs first (their code is the point), then the rest, newest first within each group.
    todo = [p for p in prs if index.get(p["url"], {}).get("updated_at") != p["updated_at"]]
    todo.sort(key=lambda p: (p["state"] != "merged", p["created_at"]), reverse=False)
    todo = sorted(todo, key=lambda p: p["state"] != "merged")
    done = 0
    try:
        for pr in todo:
            owner_repo, n = pr["repo"], pr["number"]
            entry: dict = {"updated_at": pr["updated_at"], "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
            if pr["state"] == "merged":
                files = api.pages(f"repos/{owner_repo}/pulls/{n}/files")
                record = {
                    "repo": owner_repo,
                    "number": n,
                    "url": pr["url"],
                    "title": pr["title"],
                    "merged_at": pr["merged_at"],
                    "files": [
                        {
                            "filename": f["filename"],
                            "status": f["status"],
                            "additions": f["additions"],
                            "deletions": f["deletions"],
                            "patch": f.get("patch"),  # absent for binary or very large diffs
                        }
                        for f in files
                    ],
                }
                record["test_files"] = [f["filename"] for f in files if TEST_PATH.search(f["filename"])]
                path = _detail_path(FIXES, pr)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                entry["files"] = len(files)
                entry["test_files"] = len(record["test_files"])
            reviews = api.pages(f"repos/{owner_repo}/pulls/{n}/reviews")
            review_comments = api.pages(f"repos/{owner_repo}/pulls/{n}/comments")
            comments = api.pages(f"repos/{owner_repo}/issues/{n}/comments")
            discussion = {
                "repo": owner_repo,
                "number": n,
                "url": pr["url"],
                "state": pr["state"],
                "reviews": [
                    {"role": _role(r.get("user"), r.get("author_association")), "state": r["state"],
                     "submitted_at": r.get("submitted_at"), "body": r.get("body") or ""}
                    for r in reviews
                ],
                "review_comments": [
                    {"role": _role(c.get("user"), c.get("author_association")), "path": c.get("path"),
                     "created_at": c["created_at"], "body": c.get("body") or ""}
                    for c in review_comments
                ],
                "comments": [
                    {"role": _role(c.get("user"), c.get("author_association")),
                     "created_at": c["created_at"], "body": c.get("body") or ""}
                    for c in comments
                ],
            }
            path = _detail_path(DISCUSSION, pr)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(discussion, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            people = [x for x in discussion["reviews"] + discussion["review_comments"] + discussion["comments"]
                      if x["role"] not in ("author", "bot")]
            entry["human_replies"] = len(people)
            entry["first_human_reply"] = min((x.get("submitted_at") or x.get("created_at") or "" for x in people), default=None)
            index[pr["url"]] = entry
            done += 1
    except RateLimited as stop:
        print(f"details: stopped after {done} PRs ({stop}); the next run carries on", file=sys.stderr)
    INDEX.parent.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    remaining = sum(1 for p in prs if index.get(p["url"], {}).get("updated_at") != p["updated_at"])
    print(f"details: {done} PRs fetched this run, {remaining} still to fetch, {api.calls} REST calls")
    return index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--details", action="store_true", help="also fetch the code and discussion of each PR")
    ap.add_argument("--max-calls", type=int, default=None, help="REST calls allowed for --details in this run")
    args = ap.parse_args()
    api = Api(args.max_calls)

    prs = fetch_pull_requests(api)
    issues = fetch_issues(api)
    projects = fetch_projects(api)
    repo_meta = fetch_repos(api, sorted({p["repo"] for p in prs} | {i["repo"] for i in issues}))
    for p in prs:
        meta = repo_meta.get(p["repo"].lower(), {})
        p["repo_stars"] = meta.get("stars")
        p["repo_language"] = meta.get("language")
        p["category"] = categorize(p["title"])
        if p["merged_at"]:
            created = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
            merged = datetime.fromisoformat(p["merged_at"].replace("Z", "+00:00"))
            p["hours_to_merge"] = round((merged - created).total_seconds() / 3600, 1)
        else:
            p["hours_to_merge"] = None
    for i in issues:
        i["repo_stars"] = repo_meta.get(i["repo"].lower(), {}).get("stars")

    index = fetch_details(api, prs) if args.details else (
        json.loads(INDEX.read_text(encoding="utf-8")) if INDEX.exists() else {}
    )

    merged = [p for p in prs if p["state"] == "merged"]
    closed = [p for p in prs if p["state"] == "closed"]
    by_repo = Counter(p["repo"] for p in merged)
    repos = []
    for name in sorted({p["repo"] for p in prs}, key=lambda n: -by_repo[n]):
        mine = [p for p in prs if p["repo"] == name]
        repos.append(
            {
                "repo": name,
                **repo_meta.get(name.lower(), {}),
                "merged": by_repo[name],
                "open": sum(p["state"] == "open" for p in mine),
                "closed_unmerged": sum(p["state"] == "closed" for p in mine),
                "issues": sum(i["repo"] == name for i in issues),
            }
        )
    patterns = []
    for name, rx in PATTERNS.items():
        hits = [p for p in merged if re.search(rx, p["title"], re.I)]
        patterns.append(
            {
                "pattern": name,
                "merged_fixes": len(hits),
                "repos": len({p["repo"] for p in hits}),
                "examples": [p["url"] for p in sorted(hits, key=lambda p: -(p["repo_stars"] or 0))[:5]],
            }
        )
    patterns.sort(key=lambda x: -x["merged_fixes"])
    decided = len(merged) + len(closed)
    with_code = [index[p["url"]] for p in merged if "files" in index.get(p["url"], {})]
    with_discussion = [index[p["url"]] for p in prs if p["url"] in index]
    summary = {
        "author": AUTHOR,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "pull_requests": len(prs),
        "merged": len(merged),
        "open": sum(p["state"] == "open" for p in prs),
        "closed_unmerged": len(closed),
        "merge_rate_of_decided": round(len(merged) / decided, 3) if decided else None,
        "repos_with_merges": len(by_repo),
        "merges_in_repos_10k_stars": sum((p["repo_stars"] or 0) >= 10000 for p in merged),
        "median_hours_to_merge": median(p["hours_to_merge"] for p in merged) if merged else None,
        "merged_by_month": dict(sorted(Counter(p["merged_at"][:7] for p in merged).items())),
        "merged_by_category": dict(Counter(p["category"] for p in merged).most_common()),
        "ai_disclosed_in_merged": sum(p["ai_disclosed"] for p in merged),
        "tests_mentioned_in_merged": sum(p["tests_mentioned"] for p in merged),
        "issues": len(issues),
        "issues_open": sum(i["state"] == "open" for i in issues),
        "issues_closed_completed": sum(i["state_reason"] == "completed" for i in issues),
        "projects": len(projects),
        "details": {
            "merged_with_code": len(with_code),
            "merged_whose_diff_touches_tests": sum(1 for e in with_code if e.get("test_files")),
            "prs_with_discussion": len(with_discussion),
            "prs_with_a_human_reply": sum(1 for e in with_discussion if e.get("human_replies")),
        },
    }

    DATA.mkdir(exist_ok=True)

    def dump(name: str, obj) -> None:
        (DATA / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    dump("pull_requests.json", prs)
    dump("issues.json", issues)
    dump("projects.json", projects)
    dump("repos.json", repos)
    dump("patterns.json", patterns)
    dump("summary.json", summary)
    columns = ["repo", "number", "title", "url", "state", "category", "repo_stars", "repo_language",
               "created_at", "merged_at", "closed_at", "hours_to_merge", "ai_disclosed", "tests_mentioned"]
    with open(DATA / "pull_requests.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(prs)
    print(json.dumps({k: summary[k] for k in ("pull_requests", "merged", "open", "issues", "projects", "details")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
