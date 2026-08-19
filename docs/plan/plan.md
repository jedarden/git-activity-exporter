# git-activity-exporter — plan

## Scope

Publish fleet git and bead activity as Parquet for a
`dashboard.ardenone.com/git-activity/` panel, sliceable along three
independent axes: scope (ecosystem / family / repo), measure (commits / lines
of code / beads), and time (hour / day / week).

Out of scope: per-worker attribution as a first-class tier (see Phase 4), any
write path back into a repo, and any claim about *quality* of work — this
measures volume and rhythm only.

## The question it answers

"Is the fleet more productive in bursts?" Measured against 105 local repos
over 30 days before any code was written:

- hourly-bucket Fano factor **22.0** (1.0 would be random arrival)
- busiest 10% of hours hold **34%** of all commits
- **62 of 720** hours had zero commits
- 34 discrete burst runs; the longest ran **129 hours** continuously

So: yes, decisively. But **not on a clock** — hour-of-day varies only 1.94x
across the 24 hours (cv 0.19), and weekday is flat once one large day is
removed. The bursts are episodic, tracking fleet campaigns rather than time
of day.

**This is why the primary chart is a contiguous burst timeline, not the
hour × weekday punchcard the phrase "hourly matrix" suggests.** The punchcard
ships as a secondary panel because usefully proving that negative is worth a
small tile.

The ecosystem tier was checked for meaning before being built: observed
ecosystem Fano is 34.8 against 12.8 expected if repos burst independently, so
bursts are correlated fleet-wide campaigns, not superposition of unrelated
per-repo sprints. A median burst hour spans 4 active repos, p90 9, max 44.

## Architecture

```
Forgejo  --(enumerate repos only)-->  exporter pod (ardenone-cluster)
                                        |  bare shallow mirrors on a PVC
                                        |  git log --numstat
                                        |  git show HEAD:.beads/checkpoint/forensic.jsonl
                                        v
                            s3://dashboard-site/git-activity/data/
                                        |
                                        v
                     dashboard.ardenone.com/git-activity/ (static, hyparquet)
```

One fact table at `(repo, hour)` grain carries every measure. Ecosystem and
family tiers are sums over it computed in the browser — no per-tier files, so
tiers cannot drift apart. Affordable because the grid is 1.7% dense.

## Phases

- [x] **Phase 1 — Collector.** Repo enumeration, mirror management, commit
      scan, bead event read, rollup, Parquet + meta publish, tests.
- [x] **Phase 2 — Deployment.** Namespace, ConfigMap, PVC, Deployment,
      build WorkflowTemplate, Argo Events sensor, reflector wiring.
- [x] **Phase 3 — Panel.** `public/git-activity/` with scope/measure/time
      selectors, burst timeline, punchcard, and honest captions for the bead
      epoch and the LOC filter.
- [ ] **Phase 4 — Worker tier (optional).** A fourth scope level from
      `claimed` actors. Deliberately deferred: closures are 0% attributable,
      so any worker-level "beads closed" is an inference from a claim→close
      join that breaks on release-and-reclaim. Ships only if labelled as
      inferential.

## Key decisions

**Mirrors, not the API.** Forgejo's commits endpoint costs 13.0s per 50
commits with `stat=true` versus 0.95s without — the server diffs every
commit. `git log --numstat` is free. Additionally `since`/`until` are
silently ignored on this Forgejo version, so incremental server-side
filtering is not available at any price.

**Shallow clones.** Full mirrors of all 111 repos measure 14.04 GiB. The
dashboard only reads a rolling window, so clones are bounded by
`--shallow-since`. Cold clone of NEEDLE took 148.6s / 133 MB; the subsequent
incremental fetch took 1.21s.

**Liveness and readiness are separate.** A cold start clones the whole fleet
and runs far past any sane liveness threshold. Gating liveness on first
publish would crashloop forever without completing one clone.

**Filters are flags, not deletions.** Bulk commits and bulk bead-import cells
stay in the data, marked. Raw and filtered line totals both ship. A filter
that silently removes rows is indistinguishable from missing data.

## Risks

| Risk | Mitigation |
|---|---|
| PVC fills as the fleet grows | shallow bound; `longhorn` allows expansion; watch `repos_total` |
| A repo is unreachable mid-cycle | per-repo try/except; `repos_failed` in `meta.json` |
| Families map drifts as repos are added | unmapped → `unassigned`, surfaced in `meta.json` and the panel |
| Bead threshold misfires on a future bulk import | density heuristic, not a hard-coded date; threshold configurable |
| Cold start looks like a hang | `/ready` stays 503 with progress in logs; liveness unaffected |

## Open questions

- Does lab's fleet need distinguishing from ex44's? Git carries no host
  attribution and every commit is authored `jedarden <github@jedarden.com>`,
  so host-level slicing is not available from this data at all.
- Should `commits.parquet` be retention-trimmed below `WINDOW_DAYS` if it
  outgrows a comfortable client fetch?
