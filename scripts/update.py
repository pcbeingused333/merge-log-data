#!/usr/bin/env python3
"""Rebuild the dataset from GitHub: every pull request opened by AUTHOR, its state, its repository's
stars and language, and a bug-type category for each one.

    GITHUB_TOKEN=... python scripts/update.py          # or with the gh CLI logged in

Writes data/pull_requests.json, data/pull_requests.csv, data/repos.json, data/patterns.json and
data/summary.json. Standard library only. The search API returns at most 1,000 results per query,
so the script splits the author's history into date windows that each stay under that cap.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
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


def _token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    return None


def _get(path: str, params: dict[str, str | int]) -> dict:
    url = f"https://api.github.com/{path}?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "merge-log-data"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (403, 429) and attempt < 5:
                time.sleep(min(60, 10 * (attempt + 1)))  # search API: 30 requests/minute with a token
                continue
            raise
    raise RuntimeError(f"giving up on {url}")


def _search_count(query: str) -> int:
    return _get("search/issues", {"q": query, "per_page": 1})["total_count"]


def _windows() -> list[tuple[date, date]]:
    """Date windows of PR creation, each with fewer than 1,000 results."""
    out, todo = [], [(START, date.today() + timedelta(days=1))]
    while todo:
        lo, hi = todo.pop()
        n = _search_count(f"author:{AUTHOR} type:pr created:{lo.isoformat()}..{hi.isoformat()}")
        if n == 0:
            continue
        if n < 1000 or lo == hi:
            out.append((lo, hi))
        else:
            mid = lo + (hi - lo) // 2
            todo += [(lo, mid), (mid + timedelta(days=1), hi)]
    return sorted(out)


def fetch_pull_requests() -> list[dict]:
    prs: dict[str, dict] = {}
    for lo, hi in _windows():
        query = f"author:{AUTHOR} type:pr created:{lo.isoformat()}..{hi.isoformat()}"
        for page in range(1, 11):
            items = _get("search/issues", {"q": query, "per_page": 100, "page": page})["items"]
            for it in items:
                pr = it.get("pull_request") or {}
                merged_at = pr.get("merged_at")
                state = "merged" if merged_at else it["state"]
                prs[it["html_url"]] = {
                    "repo": it["repository_url"].removeprefix("https://api.github.com/repos/"),
                    "number": it["number"],
                    "title": it["title"],
                    "url": it["html_url"],
                    "state": state,
                    "created_at": it["created_at"],
                    "merged_at": merged_at,
                    "closed_at": it.get("closed_at"),
                    "labels": [label["name"] for label in it.get("labels", [])],
                    "ai_disclosed": bool(AI_DISCLOSURE.search(it.get("body") or "")),
                    "tests_mentioned": bool(TESTS_MENTIONED.search(it.get("body") or "")),
                }
            if len(items) < 100:
                break
    return sorted(prs.values(), key=lambda p: p["created_at"], reverse=True)


def fetch_repos(names: list[str]) -> dict[str, dict]:
    repos: dict[str, dict] = {}
    batch: list[str] = []

    def flush() -> None:
        if not batch:
            return
        query = " ".join(f"repo:{n}" for n in batch)
        for it in _get("search/repositories", {"q": query, "per_page": 100})["items"]:
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


def main() -> int:
    prs = fetch_pull_requests()
    repo_meta = fetch_repos(sorted({p["repo"] for p in prs}))
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
    }

    DATA.mkdir(exist_ok=True)
    (DATA / "pull_requests.json").write_text(json.dumps(prs, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATA / "repos.json").write_text(json.dumps(repos, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATA / "patterns.json").write_text(json.dumps(patterns, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (DATA / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    columns = ["repo", "number", "title", "url", "state", "category", "repo_stars", "repo_language",
               "created_at", "merged_at", "closed_at", "hours_to_merge", "ai_disclosed", "tests_mentioned"]
    with open(DATA / "pull_requests.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(prs)
    print(json.dumps({k: summary[k] for k in ("pull_requests", "merged", "open", "closed_unmerged", "repos_with_merges")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
