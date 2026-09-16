# Configuration

All configuration is environment variables. Only `FORGE_TOKEN` and the
`DEST_S3_*` credential trio are required; everything else has a default.

| Variable | Default | Notes |
|---|---|---|
| `FORGE_BASE_URL` | `https://git.ardenone.com` | Forgejo instance |
| `FORGE_OWNER` | `jedarden` | whose repos to enumerate |
| `FORGE_TOKEN` | *(required)* | read scope is sufficient |
| `REPO_DENYLIST` | *(empty)* | comma-separated repo names to skip |
| `CLONE_ROOT` | `/data/mirrors` | must be a persistent volume |
| `WINDOW_DAYS` | `90` | reporting window |
| `SHALLOW_SINCE_DAYS` | `WINDOW_DAYS + 10` | clone depth bound |
| `TRIM_MAX_LINES` | `5000` | above this a commit is flagged bulk |
| `TRIM_MAX_FILES` | `200` | above this a commit is flagged bulk |
| `EXCLUDED_PATH_PATTERNS` | [the four defaults](#default-excluded-path-patterns) | regexes, comma-separated; replaces the default list wholesale |
| `BEAD_BULK_CLOSE_THRESHOLD` | `150` | closures per `(repo, hour)` above which the cell is flagged |
| `FAMILIES_FILE` | `families.yaml` | repo → family map |
| `POLL_INTERVAL_SECONDS` | `3600` | |
| `GIT_TIMEOUT_SECONDS` | `600` | per git invocation |
| `DEST_S3_PREFIX` | `git-activity/data` | |

## Why `SHALLOW_SINCE_DAYS` must exceed `WINDOW_DAYS`

A clone shallower than the reporting window truncates the oldest hours of
every chart with no error — the data simply is not there to find. `config.load()`
rejects that combination rather than letting it publish quietly wrong numbers.

## Chosen thresholds are measurements, not guesses

`TRIM_MAX_LINES` / `TRIM_MAX_FILES`: the largest genuine commits in a 30-day
sample sat near a 104-line median, while bulk artifact commits reached
9,072,022 lines and 25,639 files. Anything in between is comfortably
separated.

`BEAD_BULK_CLOSE_THRESHOLD`: the bead-rs migration produced `(repo, hour)`
cells up to 1,966 closures. The busiest genuine hour any repo has recorded is
69. 150 sits in the gap with room on both sides.

## Default excluded-path patterns

When `EXCLUDED_PATH_PATTERNS` is unset, these four regexes apply. Each is a
`re.search` against the repo-relative path, so a pattern fires at any depth;
a setting replaces all four, it does not append.

```
(^|/)\.beads/
(^|/)(vendor|node_modules|third_party|\.venv)/
(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum|uv\.lock|composer\.lock)$
\.(min\.js|min\.css|map)$
```

Line by line: bead checkpoint bookkeeping; vendored dependency trees;
lockfiles (machine-written, never hand-edited); minified bundles and source
maps. These are the 68.1% `.beads/` plus 5.2% vendored volume from the
2026-08-17 measurement, plus the lockfile and minified-code classes — the
filter is why `lines_*` tracks work rather than checkpoint churn, and why
`lines_*_raw` exists alongside it for auditing.

The list lives in `config.DEFAULT_EXCLUDED_PATHS`; `tests/test_docs.py`
fails if this block and the code drift apart.
