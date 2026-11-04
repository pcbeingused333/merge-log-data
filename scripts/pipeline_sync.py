#!/usr/bin/env python3
"""Refresh pipeline/runs.csv and pipeline/daily_cost.csv from the private pipeline's metric files.

    python3 scripts/pipeline_sync.py <dir with metricas.tsv and coste-historico.tsv> [--push]

Reads only numeric columns and role names, so nothing about the targets of the work (repos,
organisations, people, notes) is copied. With --push it commits and pushes when the files changed.
Standard library only; meant to run at the close of each maintenance run.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "pipeline"
ROLES = {
    "revisor": "reviewer", "fixer": "fixer", "scout": "scout", "otro": "unattributed", "moderador": "moderator",
    "correo": "follow-up owner", "vigia": "watcher", "seguimiento": "follow-up scout", "redactor": "editor",
    "consultas": "console",
}


def num(value: str | None):
    value = (value or "").strip().replace("%", "").replace(",", ".")
    if value in ("", "-", "-1"):
        return ""
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return ""


def runs(src: Path) -> list[dict]:
    out = []
    for r in csv.DictReader(open(src, encoding="utf-8"), delimiter="\t"):
        new, fup, minutes = num(r.get("pub_pr_nueva")) or 0, num(r.get("pub_followup")) or 0, num(r.get("min")) or 0
        mode = "maintenance" if new == 0 else ("continuous" if minutes >= 600 else "batch")
        out.append({
            "started": r.get("inicio", ""), "ended": r.get("fin", ""), "minutes": num(r.get("min")), "mode": mode,
            "queue_rows": num(r.get("filas")), "dropped_at_triage": num(r.get("drop_triaje")),
            "new_prs_published": new, "followups_published": fup, "merges_during_run": num(r.get("merges_tanda")),
            "merges_last_7d": num(r.get("merges_7d")), "merge_rate_30d_pct": num(r.get("tasa_merge_30d")),
            "open_prs": num(r.get("abiertas")), "sessions": num(r.get("sesiones")),
            "model_cost_usd": num(r.get("coste")), "requests": num(r.get("reqs")),
            "avg_context_tokens": num(r.get("ctx_medio")), "cost_per_published_pr_usd": num(r.get("coste_pr")),
        })
    return out


def daily(src: Path) -> list[list]:
    rows = [["date", "model_cost_usd", "requests", "usd_per_request", "avg_context_tokens", "pct_cache_reads",
             "prs_published", "new_prs", "usd_per_published_pr", "most_expensive_role"]]
    for r in csv.DictReader(open(src, encoding="utf-8"), delimiter="\t"):
        rows.append([r.get("fecha", ""), r.get("$", ""), r.get("reqs", ""), r.get("$_req", ""), r.get("ctx_medio", ""),
                     r.get("pct_lectura", ""), r.get("publicadas", ""), r.get("nuevas", ""), r.get("$_publicada", ""),
                     ROLES.get((r.get("top_rol") or "").lower(), "unattributed")])
    return rows


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    src = Path(argv[0]).expanduser()
    metrics = next((p for p in (src / "metricas.tsv", src / "drafts" / "metricas.tsv") if p.exists()), None)
    cost = next((p for p in (src / "coste-historico.tsv", src / "drafts" / "coste-historico.tsv") if p.exists()), None)
    if metrics is None:
        print(f"no metricas.tsv under {src}", file=sys.stderr)
        return 1
    OUT.mkdir(exist_ok=True)
    rows = runs(metrics)
    with open(OUT / "runs.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    if cost is not None:
        with open(OUT / "daily_cost.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(daily(cost))
    print(f"pipeline: {len(rows)} runs" + ("" if cost is None else ", daily cost"))
    if "--push" in argv:
        git = ["git", "-C", str(ROOT)]
        subprocess.run([*git, "pull", "-q", "--rebase", "--autostash"], check=False)
        subprocess.run([*git, "add", "pipeline/runs.csv", "pipeline/daily_cost.csv"], check=True)
        if subprocess.run([*git, "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run([*git, "commit", "-q", "-m", "pipeline: sync run metrics"], check=True)
            subprocess.run([*git, "push", "-q"], check=True)
            print("pipeline: pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
