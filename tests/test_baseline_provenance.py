import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts import validate_baseline
from scripts.validate_baseline import calculate


def _manifest():
    anchor = datetime(2026, 8, 17, tzinfo=timezone.utc)
    return {
        "source": {
            "repo_count": 2,
            "repositories": [
                {"name": "alpha", "head_sha": "a" * 40},
                {"name": "beta", "head_sha": "b" * 40},
            ],
        },
        "reporting": {
            "anchor_utc": anchor.isoformat().replace("+00:00", "Z"),
            "window_days": 30,
        },
        "exporter": {"version": "test", "commit": "c" * 40},
        "objects": {},
    }


def _write_snapshot(root: Path) -> Path:
    snapshot = root / "baseline-valid"
    snapshot.mkdir()
    pq.write_table(
        pa.table(
            {
                "repo": ["alpha", "alpha", "beta"],
                "sha": ["1" * 40, "2" * 40, "3" * 40],
                "ts_utc": [
                    "2026-09-27T12:00:00Z",
                    "2026-09-27T12:01:00Z",
                    "2026-09-27T12:02:00Z",
                ],
            }
        ),
        snapshot / "commits.parquet",
    )
    (snapshot / "line_totals.json").write_text(
        json.dumps({"raw_lines": 10, "beads_lines": 3, "vendored_lines": 2}),
        encoding="utf-8",
    )
    (snapshot / "ecosystem_counts.json").write_text(
        json.dumps({"counts": [0] * 720}),
        encoding="utf-8",
    )

    manifest = {
        "schema": "git-activity-baseline/v1",
        "source": {
            "forge_base_url": "https://git.ardenone.com",
            "forge_owner": "jedarden",
            "repo_count": 2,
            "repositories": [
                {"name": "alpha", "head_sha": "a" * 40},
                {"name": "beta", "head_sha": "b" * 40},
            ],
        },
        "reporting": {"anchor_utc": "2026-09-27T13:00:00Z", "window_days": 30},
        "exporter": {"version": "0.1.51", "commit": "c" * 40},
        "objects": {},
    }
    for name in validate_baseline.EXPECTED_OBJECTS:
        manifest["objects"][name] = {
            "sha256": hashlib.sha256((snapshot / name).read_bytes()).hexdigest()
        }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return snapshot


def test_baseline_calculation_zero_fills_the_half_open_window():
    manifest = _manifest()
    anchor = datetime.fromisoformat(manifest["reporting"]["anchor_utc"].replace("Z", "+00:00"))
    start = anchor - timedelta(days=30)
    rows = [
        {"repo": "alpha", "sha": "1" * 40, "ts_utc": start.isoformat().replace("+00:00", "Z")},
        {"repo": "alpha", "sha": "2" * 40, "ts_utc": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        {"repo": "beta", "sha": "3" * 40, "ts_utc": (anchor - timedelta(hours=1)).isoformat().replace("+00:00", "Z")},
        {"repo": "beta", "sha": "4" * 40, "ts_utc": anchor.isoformat().replace("+00:00", "Z")},
    ]

    result = calculate(rows, manifest, {"raw_lines": 10, "beads_lines": 3, "vendored_lines": 2}, [0] * 720)

    assert result["repo_count"] == 2
    assert result["window_hours"] == 720
    assert result["commit_count"] == 3
    assert result["zero_hours"] == 717
    assert result["burst_runs"] == 2
    assert result["longest_burst_hours"] == 2
    assert result["raw_lines"] == 10
    assert result["ecosystem_fano"] == 0


def test_full_snapshot_fixture_validates_all_contract_inputs(tmp_path):
    snapshot = _write_snapshot(tmp_path)
    manifest_path = snapshot / "manifest.json"
    manifest = validate_baseline._load_manifest(manifest_path)
    validate_baseline._check_manifest_objects(manifest, manifest_path)
    rows = validate_baseline._load_rows(snapshot / "commits.parquet")
    line_totals = validate_baseline._load_line_totals(snapshot / "line_totals.json")
    counts = validate_baseline._load_counts(snapshot / "ecosystem_counts.json", 720)

    result = calculate(rows, manifest, line_totals, counts)
    assert result["repo_count"] == 2
    assert result["window_hours"] == 720
    assert result["commit_count"] == 3
    assert result["raw_lines"] == 10
    assert result["ecosystem_fano"] == 0
    assert validate_baseline._compare(
        result,
        {
            "repo_count": {"value": 2, "tolerance": 0},
            "fano": {"value": result["fano"] + 0.01, "tolerance": 0.02},
            "ecosystem_fano": {"value": 0, "tolerance": 0},
        },
    ) == []


def test_full_snapshot_fixture_passes_the_validator_cli(tmp_path, monkeypatch):
    snapshot = _write_snapshot(tmp_path)
    expected = snapshot / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "repo_count": {"value": 2, "tolerance": 0},
                "window_hours": {"value": 720, "tolerance": 0},
                "commit_count": {"value": 3, "tolerance": 0},
                "ecosystem_fano": {"value": 0, "tolerance": 0},
                "raw_lines": {"value": 10, "tolerance": 0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_baseline.py",
            str(snapshot),
            "--line-totals",
            str(snapshot / "line_totals.json"),
            "--ecosystem-counts",
            str(snapshot / "ecosystem_counts.json"),
            "--expected",
            str(expected),
        ],
    )

    assert validate_baseline.main() == 0


def test_snapshot_fixture_rejects_changed_object_bytes(tmp_path):
    snapshot = _write_snapshot(tmp_path)
    object_path = snapshot / "line_totals.json"
    object_path.write_text(object_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    manifest_path = snapshot / "manifest.json"
    manifest = validate_baseline._load_manifest(manifest_path)

    with pytest.raises(ValueError, match="sha256 mismatch"):
        validate_baseline._check_manifest_objects(manifest, manifest_path)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda manifest: manifest.update(schema="wrong"), id="schema"),
        pytest.param(lambda manifest: manifest["source"].pop("forge_owner"), id="source-fields"),
        pytest.param(lambda manifest: manifest["source"].update(repo_count=1), id="repo-count"),
        pytest.param(
            lambda manifest: manifest["source"]["repositories"][1].update(name="alpha"),
            id="duplicate-repository-name",
        ),
        pytest.param(
            lambda manifest: manifest["source"]["repositories"][0].update(head_sha="short"),
            id="repository-head-sha",
        ),
        pytest.param(
            lambda manifest: manifest["reporting"].update(anchor_utc="2026-09-27T13:01:00Z"),
            id="reporting-anchor",
        ),
        pytest.param(lambda manifest: manifest["exporter"].update(version=""), id="exporter-version"),
        pytest.param(
            lambda manifest: manifest["exporter"].update(commit="not-a-sha"),
            id="exporter-commit",
        ),
        pytest.param(
            lambda manifest: manifest["objects"].update(
                {"../commits.parquet": manifest["objects"].pop("commits.parquet")}
            ),
            id="object-path",
        ),
        pytest.param(
            lambda manifest: manifest["objects"]["line_totals.json"].update(sha256="bad"),
            id="object-digest",
        ),
    ],
)
def test_manifest_fixture_rejects_contract_mutations(tmp_path, mutation):
    snapshot = _write_snapshot(tmp_path)
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutated = copy.deepcopy(manifest)
    mutation(mutated)
    manifest_path.write_text(json.dumps(mutated), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_baseline._load_manifest(manifest_path)


@pytest.mark.parametrize(
    "payload",
    [
        [0] * 719,
        [0] * 719 + [-1],
        [0] * 719 + [True],
        {"counts": [0] * 720, "unexpected": 1},
    ],
    ids=["short", "negative", "boolean", "unknown-field"],
)
def test_ecosystem_fixture_fails_closed_for_count_contract(tmp_path, payload):
    path = tmp_path / "ecosystem_counts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        validate_baseline._load_counts(path, 720)


@pytest.mark.parametrize(
    "check",
    [
        {"value": 1},
        {"value": 1, "tolerance": -0.01},
        {"value": 1, "tolerance": True},
        {"value": 1, "tolerance": float("nan")},
        {"value": 1, "tolerance": "0"},
    ],
    ids=["missing", "negative", "boolean", "nan", "wrong-type"],
)
def test_expected_fixture_requires_finite_non_negative_tolerances(check):
    with pytest.raises(ValueError):
        validate_baseline._compare({"fano": 1.0}, {"fano": check})


def test_expected_fixture_accepts_rounding_tolerance_and_rejects_drift():
    assert validate_baseline._compare(
        {"fano": 22.04}, {"fano": {"value": 22.0, "tolerance": 0.05}}
    ) == []
    assert validate_baseline._compare(
        {"fano": 22.06}, {"fano": {"value": 22.0, "tolerance": 0.05}}
    )
