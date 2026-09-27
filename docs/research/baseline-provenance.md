# Baseline activity provenance

This note is the provenance record for the historical numbers quoted in the
[README](../../README.md#what-the-numbers-mean), the [research plan](../plan/plan.md#the-question-it-answers),
and [prior art](prior-art.md). It also defines the refresh contract. The
checked-in validator is [`scripts/validate_baseline.py`](../../scripts/validate_baseline.py),
and the expected values copied from those documents are in
[`baseline-expected.json`](baseline-expected.json).

## What is and is not reproducible today

The published baseline was an exploratory analysis made on **2026-08-17**,
before the exporter was written, over 105 local repository mirrors and a
30-day window. The original analysis did not retain an immutable archive of
the repository listing, each repository's HEAD SHA, the exact UTC anchor, or
the per-file `git log --numstat` output. Those inputs are not present in this
repository, so the old figures must not be described as independently
rerunnable from `main` alone.

This distinction is intentional: `baseline-expected.json` is an assertion of
what the README and plan currently publish, not a fabricated source snapshot.
Once the archived inputs described below exist, the validator makes a refresh
fail closed when any published figure changes.

The baseline predates an exporter release, so its historical exporter version
is **not applicable**. The first implementation containing the measurement
semantics is commit `d2bc35f` with `VERSION` `0.1.0`. Every later refresh must
record both the checked-in `VERSION` value and the exact exporter commit in its
manifest; a date or a floating image tag is not a provenance identifier.

## Canonical input snapshot

An input snapshot is an immutable directory (and, for archival transport, a
tarball whose SHA-256 is recorded elsewhere) with this layout:

```text
baseline-<snapshot-id>/
  manifest.json
  commits.parquet
  line_totals.json
  ecosystem_counts.json
```

`commits.parquet` is the exporter commit-grain output. The snapshot manifest
must contain the following information; repository names and HEADs are part
of the input identity, not optional notes:

```json
{
  "schema": "git-activity-baseline/v1",
  "source": {
    "forge_base_url": "https://git.ardenone.com",
    "forge_owner": "jedarden",
    "repo_count": 105,
    "repositories": [
      {"name": "example", "head_sha": "<40-hex-sha>"}
    ]
  },
  "reporting": {
    "anchor_utc": "<exact RFC3339 UTC hour>",
    "window_days": 30
  },
  "exporter": {
    "version": "<contents of VERSION>",
    "commit": "<40-hex git SHA>"
  },
  "objects": {
    "commits.parquet": {"sha256": "<64-hex-sha256>"},
    "line_totals.json": {"sha256": "<64-hex-sha256>"},
    "ecosystem_counts.json": {"sha256": "<64-hex-sha256>"}
  }
}
```

The manifest's `objects` paths are relative to the manifest and are checked by
the validator before any calculation. `ecosystem_counts.json` is exactly 720
non-negative integer counts. It is separate from the primary commit series
because the plan's “ecosystem Fano 34.8” claim did not preserve its activity
selector; the snapshot must name that selector in its archival record before
that secondary claim is treated as reproducible. It must never be silently
compared with the primary commit Fano of 22.0.

To create a new snapshot, complete the Forgejo repository enumeration first,
record the full result and every repository HEAD, run the pinned exporter
against those mirrors, then copy the immutable cycle's `commits.parquet` and
its `meta.json`/`current.json` metadata into the archive. Generate the
path-level totals from the same repository HEADs before releasing the archive;
do not fetch “latest” again during analysis. A manifest object digest is
recorded with:

```bash
sha256sum commits.parquet line_totals.json ecosystem_counts.json
```

No credential, token, or signed Forgejo URL belongs in the snapshot or its
manifest.

## Source data and inclusion rules

The source is the completed `GET /api/v1/repos/search` listing for
`FORGE_OWNER`, followed by each repository's bare Git mirror. Forgejo's
commits API is not a source of measurements. For each pinned HEAD, commit rows
come from the equivalent of:

```text
git log --all --no-merges --numstat
```

The timestamp is the Git **author** timestamp (`%at`), not fetch time,
committer time, or publication time. A merge is excluded because it has no
numstat row in the exporter. Binary files count as touched files and add zero
lines. Every included commit remains a commit even when its line contribution
is filtered or flagged as bulk.

Bead data, where used for bead figures, is the exact
`.beads/checkpoint/forensic.jsonl` blob (and matching
`.beads/checkpoint/current.json` manifest when present) at each pinned HEAD.
It is validated before filtering, then event timestamps are filtered into the
same window. Missing forensic files mean no bead observations; they are not
invented from claims or commits. The migration epoch begins 2026-08-14 and
bulk-import closures remain visible and flagged.

For LOC, path matching uses the committed defaults in
[`src/config.py`](../../src/config.py) with `re.search`:

```text
(^|/)\.beads/
(^|/)(vendor|node_modules|third_party|\.venv)/
(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum|uv\.lock|composer\.lock)$
\.(min\.js|min\.css|map)$
```

`line_totals.json` is retained because `commits.parquet` intentionally stores
totals, not every changed path. It must include at least `raw_lines`,
`beads_lines`, and `vendored_lines`, calculated from the same per-file
numstat rows. The exporter calculates `is_bulk` **after** path filtering:

```text
is_bulk = (lines_added + lines_deleted) > TRIM_MAX_LINES
          OR files_changed > TRIM_MAX_FILES
```

The baseline defaults are `TRIM_MAX_LINES=5000` and `TRIM_MAX_FILES=200`;
both comparisons are strict. A bulk commit counts in the commit series and
its raw line totals, but contributes zero to filtered hourly LOC. For bead
closures, the migration flag uses `BEAD_BULK_CLOSE_THRESHOLD=150` per
`(repo, hour)` plus `BEAD_BULK_HOUR_SHARE=0.5` for a contaminated fleet hour;
flagging is an annotation, not deletion.

## Reporting window and buckets

The manifest's exact `anchor_utc` is the sole end of the half-open window:

```text
[anchor_utc - 30 days, anchor_utc)
```

The published baseline is 30 × 24 = **720 UTC hours**. The lower boundary is
included and the anchor is excluded. Timestamps are normalized to UTC before
the comparison; no local timezone or daylight-saving adjustment is involved.
The bucket is integer Unix-hour division:

```text
hour = floor(unix_seconds(timestamp) / 3600)
```

The validator zero-fills all 720 buckets, including the 62 empty hours, and
uses the event's source timestamp rather than the mirror or exporter clock.

## Fano, burst, and related definitions

For an hourly count vector `x` of length 720, the Fano factor is population
variance divided by arithmetic mean:

```text
Fano(x) = (sum((x[i] - mean(x))²) / 720) / mean(x)
```

The primary baseline vector is all included commit rows, summed across the
105 repositories. Fano = 1 is the Poisson/random-arrival reference. The
busiest 10% is the 72 largest hourly counts (ties are included only through
that fixed 72-hour cutoff), divided by total commits.

A burst run is a maximal contiguous sequence of hourly buckets with at least
one commit. The validator reports the number of runs and the longest run;
there is no gap tolerance and empty hours terminate a run. Hour-of-day spread
is `max(hour_of_day_total) / min(hour_of_day_total)` over 24 UTC hour labels,
and its CV is population standard deviation divided by the mean. Active-repo
statistics are computed only for non-empty hours: median, nearest-rank p90,
and maximum of the number of distinct repositories contributing a commit.

The independent-repository comparison is the variance-sum null:

```text
expected_independent_fano = sum(population_variance(repo_hour_counts))
                            / sum(mean(repo_hour_counts))
```

It assumes repository hour series are independent while preserving each
repository's observed burstiness. Any separately published ecosystem series
must be supplied as `ecosystem_counts.json` and named in the snapshot record;
its Fano is calculated by the same variance/mean formula.

## Validation and refresh policy

After extracting an immutable snapshot, run from this repository revision:

```bash
python scripts/validate_baseline.py /path/to/baseline-<snapshot-id> \
  --line-totals /path/to/baseline-<snapshot-id>/line_totals.json \
  --ecosystem-counts /path/to/baseline-<snapshot-id>/ecosystem_counts.json \
  --expected docs/research/baseline-expected.json
```

The command verifies manifest object hashes, repository membership and count,
the 30-day/720-hour window, then prints the computed report. With the
historical input archive it must match the expected values copied from the
README and plan, including Fano 22.0, 34% in the busiest 10%, 62 zero hours,
34 runs, a 129-hour maximum run, 1.94× hour-of-day spread, CV 0.19,
independent-repository Fano 12.8, ecosystem Fano 34.8, and the three LOC
totals 61,083,980 / 41,626,557 / 3,180,287. The expected JSON stores explicit
tolerances for rounded values and exact equality for counts.

Refresh policy:

1. Keep the old snapshot, manifest, expected file, and published prose
   unchanged while collecting a new snapshot.
2. Pin and record the new exporter `VERSION` plus Git commit, exact anchor,
   repository listing/HEADs, object SHA-256 values, and any changed filter or
   bulk thresholds.
3. Run the validator. A mismatch is a review stop: determine whether the
   source fleet, window, selector, exporter semantics, or published claim
   changed. Do not round a mismatch away or overwrite the expected file first.
4. If the baseline is intentionally refreshed, update the expected file and
   README/plan together, linking the snapshot ID and validator command in the
   same change. Retain the previous snapshot so the old claim remains
   auditable.
