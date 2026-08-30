# git-activity-exporter

> **Internal tooling, public for reference.** The exporter is currently wired
> to the author's Forgejo estate. Reusers must provide their own owner, family
> mapping, storage destination, and deployment configuration.

Exports fleet git and bead activity as Parquet for the
`dashboard.ardenone.com/git-activity/` panel.

Polls every repo owned by a Forgejo user, keeps a bare shallow mirror of each
on a PVC, and publishes three objects to an S3 prefix each cycle:

| Object | Grain | Purpose |
|---|---|---|
| `hourly.parquet` | `(repo, hour)` | every scope tier derives from this one table |
| `commits.parquet` | commit | drill-down detail |
| `bead_events.parquet` | event | bead lifecycle detail |
| `meta.json` | — | freshness, coverage, and the caveats the panel must display |

## Three axes of granularity

- **Scope** — ecosystem → family → repo. All three are sums over
  `hourly.parquet`, computed in the browser. There are no per-tier files,
  so tiers cannot disagree with each other.
- **Measure** — commits · lines of code · beads.
- **Time** — hour → day → week, rolled up client-side.

Families are editorial and live in `families.yaml`; unmapped repos fall
through to `unassigned` and still appear in the ecosystem and repo tiers.

## What the numbers mean

Two filters are not optional, and both are visible in the output rather than
applied silently.

**Lines of code excludes machine-generated paths.** Measured 2026-08-17 over
30 days and 105 repos: of 61,083,980 lines changed, `.beads/` bookkeeping was
41,626,557 (68.1%) and vendored trees a further 3,180,287 (5.2%). Only 26.5%
of raw line volume is plausibly hand-written. Both `lines_*` (filtered) and
`lines_*_raw` (unfiltered) ship, so the filter is auditable.

**Bead closures carry a migration artifact.** Every forensic log in the fleet
begins 2026-08-14, the bead-rs migration, and three hours that day hold 87%
of all closure events. Dense `(repo, hour)` cells are flagged
`is_bulk_import` rather than deleted. `meta.json` carries `bead_epoch_utc` so
the panel can caption the bead charts honestly instead of drawing an empty
left half.

## Why mirrors instead of the API

Measured against Forgejo 10.0.0+gitea-1.22.0 on 2026-08-17: a page of 50
commits costs 0.95s without diff stats and 13.0s with `stat=true` — about
14x, because the server diffs every commit. A 30-day backfill of ~14k commits
would be roughly an hour of pure diffing. `git log --numstat` against a local
mirror gives the same numbers for free. The API is used only to enumerate
repos.

`since`/`until` on that endpoint are **silently ignored** on this version
(verified: `until=2026-08-15` returned commits from 2026-08-17), so
server-side incremental filtering is not available even if the cost were
acceptable.

## Configuration

See `docs/notes/configuration.md`. Treat the committed defaults and
`families.yaml` as the author's example deployment; set `FORGE_BASE_URL`,
`FORGE_OWNER`, and your own family mapping in addition to the required
`FORGE_TOKEN` and `DEST_S3_*` credentials.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
