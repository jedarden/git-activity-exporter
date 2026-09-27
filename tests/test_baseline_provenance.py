from datetime import datetime, timedelta, timezone

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
