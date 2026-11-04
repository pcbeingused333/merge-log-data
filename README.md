# merge-log-data

Every pull request opened by [@pcbeingused333](https://github.com/pcbeingused333) on GitHub, with its
outcome, its repository and a bug-type category: the data behind the
[Merge Log](https://portfolio-alexgonzalez33.vercel.app).

Most of these pull requests come from a multi-agent pipeline I designed and run: scouts find bugs
users hit in current `main`, fixers reproduce and fix them with a failing-first test, and an
independent reviewer re-runs that test before anything is published. How it works, its modes
(including the overnight maintenance runs on open PRs) and its run-by-run numbers are in
[`pipeline/`](pipeline/).

## Snapshot (2026-11-03)

| | |
| --- | --- |
| Pull requests opened | 1,425 |
| Merged by maintainers | 742 |
| Open | 573 |
| Closed without merging | 110 |
| Merge rate of those decided | 87% |
| Repositories with merged work | 170 |
| Merges into repositories with 10k+ stars | 168 |
| Median time to merge | 17.6 hours |

`data/summary.json` holds the current numbers; the table above is the snapshot of the first release.

## Recurring bug patterns

The same mistakes show up across the AI tooling ecosystem. Merged fixes per pattern, from
`data/patterns.json`:

| Pattern | Merged fixes | Repos |
| --- | --- | --- |
| Defaults pointing at retired models | 67 | 42 |
| Unknown or unmapped finish / stop reasons | 41 | 18 |
| Cache read and write tokens miscounted | 26 | 18 |
| Streamed parallel tool calls merged when the provider omits an index | 25 | 24 |
| Sampling parameters sent to Claude models that reject them | 15 | 12 |
| Reasoning / thinking tokens missing from usage | 13 | 13 |
| Anthropic cumulative `message_delta` usage | 9 | 8 |

Several of these became rules in [llm-drift-lint](https://github.com/pcbeingused333/llm-drift-lint),
each tested against the real files from these fixes.

## Files

| File | What it holds |
| --- | --- |
| `fixes/<owner>/<repo>/<number>.json` | The code of each merged PR: every file it changed with its diff, and which of them are tests |
| `discussion/<owner>/<repo>/<number>.json` | Reviews, review comments and comments on each PR. Authors appear only by role (`author`, `member`, `owner`, `contributor`, `bot`), never by name |
| `data/issues.json` | Every issue opened, with its state |
| `data/projects.json` | My own public repositories |
| `pipeline/` | How the pipeline works and `runs.csv`, one row per run |
| `data/pull_requests.json` / `.csv` | One row per pull request: repo, number, title, url, state (`merged` / `open` / `closed`), category, repo stars and language, created / merged / closed timestamps, hours to merge, whether the description discloses AI use and mentions tests |
| `data/repos.json` | One row per repository: stars, language, description, and how many of the pull requests were merged, are open or were closed |
| `data/patterns.json` | The recurring patterns above, with up to five example pull requests each |
| `data/summary.json` | Totals, merges per month and per category |

Categories and patterns are assigned from the pull request title with the rules at the top of
`scripts/update.py`, so they are approximate: a title that matches no rule is "Application logic".

## Updating

```bash
python scripts/update.py                   # PRs, issues, projects (search API)
python scripts/update.py --details         # also fixes/ and discussion/ (REST, incremental)
```

A token comes from `GITHUB_TOKEN` or a logged-in `gh`. A GitHub Actions workflow runs it with
`--details` every six hours, capped at 900 REST calls a run, and commits what changed. The first
backfill of `fixes/` and `discussion/` takes about two days of runs; after that only new or
updated PRs are fetched. `pipeline/runs.csv` is refreshed from the private system at the start
of each maintenance run with `scripts/pipeline_sync.py`, run from a private copy.

## License

Code: MIT. The data describes public pull requests; titles belong to their authors and the
repositories they were opened in.
