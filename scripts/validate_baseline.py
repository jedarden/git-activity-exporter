#!/usr/bin/env python3
"""Validate the historical baseline against an immutable exporter snapshot.

The baseline snapshot is deliberately supplied as input rather than checked in:
it contains the fleet's commit rows and repository HEADs.  The manifest pins
the source, reporting anchor, exporter revision, and object digests.  This
script only computes statistics; it never contacts Forgejo or S3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


EXPECTED_PATH_TOTALS = {
    "raw_lines": "raw_lines",
    "beads_lines": "beads_lines",
    "vendored_lines": "vendored_lines",
}


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamps must be RFC 3339 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"timestamp has no UTC offset: {value}")
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    for key in ("source", "reporting", "exporter", "objects"):
        if not isinstance(manifest.get(key), dict):
            raise ValueError(f"manifest.{key} must be an object")
    reporting = manifest["reporting"]
    if reporting.get("window_days") != 30:
        raise ValueError("the published baseline requires reporting.window_days = 30")
    anchor = _parse_timestamp(reporting["anchor_utc"])
    if anchor.minute or anchor.second or anchor.microsecond:
        raise ValueError("the published baseline anchor must be on a UTC hour")
    return manifest


def _load_rows(path: Path) -> list[dict[str, Any]]:
    table = pq.read_table(path, columns=["repo", "sha", "ts_utc"])
    rows = table.to_pylist()
    seen = set()
    for row in rows:
        if not isinstance(row.get("repo"), str) or not isinstance(row.get("sha"), str):
            raise ValueError("commits.parquet has a non-string repo or sha")
        identity = (row["repo"], row["sha"])
        if identity in seen:
            raise ValueError(f"commits.parquet repeats commit identity {identity[0]}:{identity[1]}")
        seen.add(identity)
        _parse_timestamp(row["ts_utc"])
    return rows


def _hour_counts(rows: list[dict[str, Any]], start: datetime, hours: int) -> tuple[list[int], list[int]]:
    counts = [0] * hours
    repos = [set() for _ in range(hours)]
    for row in rows:
        ts = _parse_timestamp(row["ts_utc"])
        if start <= ts < start + timedelta(hours=hours):
            offset = int((ts - start).total_seconds() // 3600)
            counts[offset] += 1
            repos[offset].add(row["repo"])
    return counts, [len(active) for active in repos]


def _fano(counts: list[int]) -> float:
    mean = statistics.fmean(counts)
    if mean == 0:
        return 0.0
    return statistics.pvariance(counts) / mean


def _burst_lengths(counts: list[int]) -> list[int]:
    lengths = []
    current = 0
    for count in counts:
        if count:
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered))) - 1
    return float(ordered[rank])


def _repo_fano(rows: list[dict[str, Any]], repos: list[str], start: datetime, hours: int) -> float:
    by_repo = {repo: [0] * hours for repo in repos}
    for row in rows:
        ts = _parse_timestamp(row["ts_utc"])
        if start <= ts < start + timedelta(hours=hours):
            offset = int((ts - start).total_seconds() // 3600)
            by_repo.setdefault(row["repo"], [0] * hours)[offset] += 1
    numerator = sum(statistics.pvariance(counts) for counts in by_repo.values())
    denominator = sum(statistics.fmean(counts) for counts in by_repo.values())
    return numerator / denominator if denominator else 0.0


def _load_counts(path: Path, hours: int) -> list[int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts = payload.get("counts") if isinstance(payload, dict) else payload
    if not isinstance(counts, list) or len(counts) != hours:
        raise ValueError(f"{path} must contain exactly {hours} hourly counts")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError(f"{path} contains a non-negative integer count that is invalid")
    return counts


def calculate(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    line_totals: dict[str, Any] | None = None,
    ecosystem_counts: list[int] | None = None,
) -> dict[str, Any]:
    reporting = manifest["reporting"]
    anchor = _parse_timestamp(reporting["anchor_utc"])
    hours = reporting["window_days"] * 24
    start = anchor - timedelta(days=reporting["window_days"])
    counts, active_repos = _hour_counts(rows, start, hours)
    bursts = _burst_lengths(counts)
    top_n = math.ceil(hours * 0.10)
    total_commits = sum(counts)
    top_share = sum(sorted(counts, reverse=True)[:top_n]) / total_commits if total_commits else 0.0
    hour_of_day = [sum(counts[offset] for offset in range(hour, hours, 24)) for hour in range(24)]
    hour_mean = statistics.fmean(hour_of_day)

    result: dict[str, Any] = {
        "repo_count": manifest["source"].get("repo_count"),
        "window_hours": hours,
        "commit_count": total_commits,
        "fano": _fano(counts),
        "top_10_share": top_share,
        "zero_hours": counts.count(0),
        "burst_runs": len(bursts),
        "longest_burst_hours": max(bursts, default=0),
        "hour_of_day_spread": max(hour_of_day) / min(hour_of_day) if min(hour_of_day) else 0.0,
        "hour_of_day_cv": statistics.pstdev(hour_of_day) / hour_mean if hour_mean else 0.0,
        "independent_repo_fano": _repo_fano(
            rows,
            [repo["name"] for repo in manifest["source"].get("repositories", [])],
            start,
            hours,
        ),
        "median_active_repos": statistics.median(active for active, count in zip(active_repos, counts) if count),
        "p90_active_repos": _percentile([active for active, count in zip(active_repos, counts) if count], 0.90),
        "max_active_repos": max(active_repos, default=0),
    }
    if line_totals:
        result.update({key: line_totals[value] for key, value in EXPECTED_PATH_TOTALS.items()})
    if ecosystem_counts is not None:
        result["ecosystem_fano"] = _fano(ecosystem_counts)
    return result


def _check_manifest_objects(manifest: dict[str, Any], manifest_path: Path) -> None:
    for name, descriptor in manifest["objects"].items():
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("sha256"), str):
            raise ValueError(f"manifest.objects.{name} must contain sha256")
        path = manifest_path.parent / name
        if not path.is_file():
            raise ValueError(f"manifest object is missing: {path}")
        actual = _sha256(path)
        if actual != descriptor["sha256"]:
            raise ValueError(f"sha256 mismatch for {name}: expected {descriptor['sha256']}, got {actual}")


def _compare(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    failures = []
    for name, check in expected.items():
        if not isinstance(check, dict) or "value" not in check:
            raise ValueError(f"expected.{name} must contain value and tolerance")
        if name not in result:
            failures.append(f"{name}: result is missing (supply the path-level line totals)")
            continue
        tolerance = check.get("tolerance", 0)
        actual = result[name]
        wanted = check["value"]
        if isinstance(actual, (int, float)) and isinstance(wanted, (int, float)):
            matches = math.isclose(actual, wanted, rel_tol=0.0, abs_tol=tolerance)
        else:
            matches = actual == wanted
        if not matches:
            failures.append(f"{name}: expected {wanted} ± {tolerance}, got {actual}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="directory containing manifest.json and commits.parquet")
    parser.add_argument("--expected", type=Path, help="JSON object of metric checks")
    parser.add_argument("--line-totals", type=Path, help="JSON object with raw_lines, beads_lines, and vendored_lines")
    parser.add_argument(
        "--ecosystem-counts",
        type=Path,
        help="JSON list/object of the separately defined ecosystem series, one count per UTC hour",
    )
    args = parser.parse_args()

    manifest_path = args.snapshot / "manifest.json"
    commits_path = args.snapshot / "commits.parquet"
    manifest = _load_manifest(manifest_path)
    _check_manifest_objects(manifest, manifest_path)
    rows = _load_rows(commits_path)
    repositories = manifest["source"].get("repositories", [])
    expected_repos = {repo["name"] for repo in repositories}
    actual_repos = {row["repo"] for row in rows}
    if expected_repos and not actual_repos <= expected_repos:
        unknown = sorted(actual_repos - expected_repos)
        raise ValueError(f"commits.parquet contains repos absent from manifest: {unknown}")
    if manifest["source"].get("repo_count") != len(expected_repos or actual_repos):
        raise ValueError("manifest source.repo_count does not match its repository list")

    line_totals = None
    if args.line_totals:
        line_totals = json.loads(args.line_totals.read_text(encoding="utf-8"))
    hours = manifest["reporting"]["window_days"] * 24
    ecosystem_counts = _load_counts(args.ecosystem_counts, hours) if args.ecosystem_counts else None
    result = calculate(rows, manifest, line_totals, ecosystem_counts)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        failures = _compare(result, expected)
        if failures:
            raise SystemExit("baseline validation failed:\n" + "\n".join(f"- {failure}" for failure in failures))
        print("baseline validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
