#!/usr/bin/env python3
"""Refresh pipeline/runs.csv from the private pipeline's metric file.

    python3 pipeline_sync.py <dir with metricas.tsv> --repo <clone of merge-log-data> [--push]

Copy this file somewhere private (e.g. ~/bin) and run it from there: it then never executes code
pulled from the public repository, which it only uses for data. It reads numeric columns only,
so nothing about the targets of the work (repos, organisations, people, notes) is copied, and
it publishes the cost per published PR, not totals. With --push it commits and pushes when the
file changed. Standard library only; meant to run at the start of each maintenance run.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

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
            "avg_context_tokens": num(r.get("ctx_medio")), "cost_per_published_pr_usd": num(r.get("coste_pr")),
        })
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    if "--repo" not in argv or argv.index("--repo") + 1 >= len(argv):
        print("--repo <clone of merge-log-data> is required", file=sys.stderr)
        return 2
    root = Path(argv[argv.index("--repo") + 1]).expanduser().resolve()
    out = root / "pipeline"
    src = Path(argv[0]).expanduser()
    metrics = next((p for p in (src / "metricas.tsv", src / "drafts" / "metricas.tsv") if p.exists()), None)
    if metrics is None:
        print(f"no metricas.tsv under {src}", file=sys.stderr)
        return 1
    out.mkdir(exist_ok=True)
    rows = runs(metrics)
    with open(out / "runs.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"pipeline: {len(rows)} runs")
    if "--push" in argv:
        git = ["git", "-C", str(root)]
        subprocess.run([*git, "pull", "-q", "--rebase", "--autostash"], check=False)
        subprocess.run([*git, "add", "pipeline/runs.csv"], check=True)
        if subprocess.run([*git, "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run([*git, "commit", "-q", "-m", "pipeline: sync run metrics"], check=True)
            subprocess.run([*git, "push", "-q"], check=True)
            print("pipeline: pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
