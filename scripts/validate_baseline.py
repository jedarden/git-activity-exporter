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
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pyarrow.parquet as pq


EXPECTED_PATH_TOTALS = {
    "raw_lines": "raw_lines",
    "beads_lines": "beads_lines",
    "vendored_lines": "vendored_lines",
}
BASELINE_SCHEMA = "git-activity-baseline/v1"
EXPECTED_OBJECTS = ("commits.parquet", "line_totals.json", "ecosystem_counts.json")
SHA1_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
ANCHOR_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:00:00Z\Z")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing {missing}")
        if unknown:
            details.append(f"unknown {unknown}")
        raise ValueError(f"{label} has invalid fields: {', '.join(details)}")


def _validate_repo_name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty repository name")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must not be a path")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} contains a control character")


def _validate_sha(value: Any, label: str) -> None:
    if not isinstance(value, str) or SHA1_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a 40-character hexadecimal SHA")


def _validate_relative_object_path(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a relative object path")
    path = Path(value)
    if path.is_absolute() or path.anchor or ".." in path.parts:
        raise ValueError(f"{label} must be a relative object path")


def _validate_tolerance(value: Any, label: str) -> None:
    if not _is_number(value) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number")


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
    _require_exact_keys(manifest, {"schema", "source", "reporting", "exporter", "objects"}, "manifest")
    if manifest["schema"] != BASELINE_SCHEMA:
        raise ValueError(f"manifest.schema must be {BASELINE_SCHEMA!r}")

    source = manifest["source"]
    if not isinstance(source, dict):
        raise ValueError("manifest.source must be an object")
    _require_exact_keys(source, {"forge_base_url", "forge_owner", "repo_count", "repositories"}, "manifest.source")
    forge_url = source["forge_base_url"]
    parsed_url = urlsplit(forge_url) if isinstance(forge_url, str) else None
    if (
        parsed_url is None
        or parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError("manifest.source.forge_base_url must be an HTTPS base URL")
    if not isinstance(source["forge_owner"], str) or not source["forge_owner"].strip():
        raise ValueError("manifest.source.forge_owner must be a non-empty string")
    if not _is_integer(source["repo_count"]) or source["repo_count"] < 0:
        raise ValueError("manifest.source.repo_count must be a non-negative integer")
    repositories = source["repositories"]
    if not isinstance(repositories, list):
        raise ValueError("manifest.source.repositories must be a list")
    repository_names = set()
    for index, repository in enumerate(repositories):
        label = f"manifest.source.repositories[{index}]"
        if not isinstance(repository, dict):
            raise ValueError(f"{label} must be an object")
        _require_exact_keys(repository, {"name", "head_sha"}, label)
        _validate_repo_name(repository["name"], f"{label}.name")
        _validate_sha(repository["head_sha"], f"{label}.head_sha")
        if repository["name"] in repository_names:
            raise ValueError(f"manifest source repeats repository {repository['name']!r}")
        repository_names.add(repository["name"])
    if source["repo_count"] != len(repositories):
        raise ValueError("manifest source.repo_count does not match its repository list")

    reporting = manifest["reporting"]
    if not isinstance(reporting, dict):
        raise ValueError("manifest.reporting must be an object")
    _require_exact_keys(reporting, {"anchor_utc", "window_days"}, "manifest.reporting")
    if reporting.get("window_days") != 30:
        raise ValueError("the published baseline requires reporting.window_days = 30")
    anchor_value = reporting.get("anchor_utc")
    if not isinstance(anchor_value, str) or ANCHOR_RE.fullmatch(anchor_value) is None:
        raise ValueError("manifest.reporting.anchor_utc must be an RFC 3339 UTC hour")
    anchor = _parse_timestamp(reporting["anchor_utc"])
    if anchor.minute or anchor.second or anchor.microsecond:
        raise ValueError("the published baseline anchor must be on a UTC hour")

    exporter = manifest["exporter"]
    if not isinstance(exporter, dict):
        raise ValueError("manifest.exporter must be an object")
    _require_exact_keys(exporter, {"version", "commit"}, "manifest.exporter")
    if not isinstance(exporter["version"], str) or not exporter["version"].strip():
        raise ValueError("manifest.exporter.version must be a non-empty string")
    _validate_sha(exporter["commit"], "manifest.exporter.commit")

    objects = manifest["objects"]
    if not isinstance(objects, dict):
        raise ValueError("manifest.objects must be an object")
    for name in objects:
        _validate_relative_object_path(name, f"manifest.objects.{name!r}")
    if set(objects) != set(EXPECTED_OBJECTS):
        raise ValueError(f"manifest.objects must contain exactly {list(EXPECTED_OBJECTS)!r}")
    for name, descriptor in objects.items():
        if not isinstance(descriptor, dict):
            raise ValueError(f"manifest.objects.{name} must be an object")
        _require_exact_keys(descriptor, {"sha256"}, f"manifest.objects.{name}")
        digest = descriptor["sha256"]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"manifest.objects.{name}.sha256 must be a 64-character hexadecimal digest")
    return manifest


def _load_rows(path: Path) -> list[dict[str, Any]]:
    table = pq.read_table(path, columns=["repo", "sha", "ts_utc"])
    rows = table.to_pylist()
    seen = set()
    for row in rows:
        _validate_repo_name(row.get("repo"), "commits.parquet.repo")
        _validate_sha(row.get("sha"), "commits.parquet.sha")
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
    if isinstance(payload, dict):
        _require_exact_keys(payload, {"counts"}, str(path))
        counts = payload["counts"]
    else:
        counts = payload
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
        relative = Path(name)
        root = manifest_path.parent.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError(f"manifest object escapes snapshot: {name}") from error
        if not path.is_file():
            raise ValueError(f"manifest object is missing: {path}")
        actual = _sha256(path)
        if actual.lower() != descriptor["sha256"].lower():
            raise ValueError(f"sha256 mismatch for {name}: expected {descriptor['sha256']}, got {actual}")


def _load_line_totals(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    for name in EXPECTED_PATH_TOTALS.values():
        value = payload.get(name)
        if not _is_integer(value) or value < 0:
            raise ValueError(f"{path}.{name} must be a non-negative integer")
    return payload


def _require_manifest_object_argument(name: str, supplied: Path, manifest_path: Path) -> None:
    expected = (manifest_path.parent / name).resolve()
    if supplied.resolve() != expected:
        raise ValueError(f"{name} must be loaded from the manifest object path {expected}")


def _compare(result: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    if not isinstance(expected, dict):
        raise ValueError("expected values must be a JSON object")
    failures = []
    for name, check in expected.items():
        if not isinstance(check, dict) or set(check) != {"value", "tolerance"}:
            raise ValueError(f"expected.{name} must contain value and tolerance")
        tolerance = check["tolerance"]
        _validate_tolerance(tolerance, f"expected.{name}.tolerance")
        wanted = check["value"]
        if not _is_number(wanted) or not math.isfinite(wanted):
            raise ValueError(f"expected.{name}.value must be a finite number")
        if name not in result:
            failures.append(f"{name}: result is missing (supply the path-level line totals)")
            continue
        actual = result[name]
        if _is_number(actual):
            if not math.isfinite(actual):
                raise ValueError(f"expected.{name}.value and result must be finite")
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
        _require_manifest_object_argument("line_totals.json", args.line_totals, manifest_path)
        line_totals = _load_line_totals(args.line_totals)
    hours = manifest["reporting"]["window_days"] * 24
    if args.ecosystem_counts:
        _require_manifest_object_argument("ecosystem_counts.json", args.ecosystem_counts, manifest_path)
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
