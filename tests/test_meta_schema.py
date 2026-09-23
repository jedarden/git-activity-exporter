"""The meta.json contract, executable.

docs/notes/output-schema.md states the contract in prose and a table; this
module is the same contract in a form CI can enforce. The documented example,
the documented field table and main.build_meta's real output are all checked
against it, alongside fixtures pinning the guarantees a consumer is told to
rely on: coverage arithmetic, failed/stale disjointness, repo_errors keying,
the null bead_epoch_utc case. A field cannot appear, vanish, change type, or
lose one of those guarantees without failing here first.
"""
import json
import re
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import main

OUTPUT_SCHEMA_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "output-schema.md"
)

RFC3339_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CYCLE_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")

Field = namedtuple("Field", "type nullable")

# The type vocabulary: string, int (a bool is not an int here), number (int
# or float, bool excluded), string list, string map. Order is build_meta's
# insertion order, which the field table in output-schema.md mirrors.
META_CONTRACT = {
    "version": Field("string", False),
    "cycle_id": Field("string", False),
    "generated_at": Field("string", False),
    "window_days": Field("int", False),
    "repos_total": Field("int", False),
    "repos_scanned": Field("int", False),
    "repos_failed": Field("string list", False),
    "repo_errors": Field("string map", False),
    "repos_stale": Field("string list", False),
    "mirrors_pruned": Field("string list", False),
    "repos_with_bead_data": Field("int", False),
    "git_timeout_seconds": Field("int", False),
    "cycle_seconds": Field("number", False),
    "bead_epoch_utc": Field("string", True),
    "attribution_epoch": Field("string map", False),
    "bulk_bead_cells": Field("int", False),
    "unassigned_repos": Field("string list", False),
    "trim_max_lines": Field("int", False),
    "trim_max_files": Field("int", False),
    "excluded_path_patterns": Field("string list", False),
}

_POSITIVE_INTS = ("window_days", "git_timeout_seconds", "trim_max_lines", "trim_max_files")
_REPO_LISTS = ("repos_failed", "repos_stale", "mirrors_pruned", "excluded_path_patterns")


def _type_ok(value, kind):
    if kind == "string":
        return isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "string list":
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    if kind == "string map":
        return isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        )
    raise AssertionError(f"unknown contract type: {kind}")


def validate_meta(meta):
    """The contract violations in meta, one human-readable string each.

    Cross-field rules are guarded so a wrong-typed value reports its type
    violation instead of crashing the validator halfway through.
    """
    bad = []
    for name in sorted(set(META_CONTRACT) - set(meta)):
        bad.append(f"missing required field: {name}")
    for name in sorted(set(meta) - set(META_CONTRACT)):
        bad.append(f"unknown field: {name}")

    for name, spec in META_CONTRACT.items():
        if name not in meta:
            continue
        value = meta[name]
        if value is None:
            if not spec.nullable:
                bad.append(f"{name} is null and the field is never null")
            continue
        if not _type_ok(value, spec.type):
            bad.append(f"{name} is not of contract type {spec.type!r}")

    generated_at, cycle_id = meta.get("generated_at"), meta.get("cycle_id")
    if isinstance(generated_at, str):
        if not RFC3339_Z.match(generated_at):
            bad.append("generated_at must be RFC 3339 UTC with an explicit Z")
        if isinstance(cycle_id, str) and not cycle_id.startswith(
            generated_at.replace("-", "").replace(":", "")
        ):
            bad.append("cycle_id must name the cycle's generated_at")
    if isinstance(cycle_id, str) and not CYCLE_ID.match(cycle_id):
        bad.append("cycle_id must be <compacted generated_at>-<8 hex>")
    epoch = meta.get("bead_epoch_utc")
    if isinstance(epoch, str) and not RFC3339_Z.match(epoch):
        bad.append("bead_epoch_utc must be RFC 3339 UTC with an explicit Z")
    attribution = meta.get("attribution_epoch")
    if isinstance(attribution, dict):
        for repo, value in attribution.items():
            if not isinstance(value, str) or not RFC3339_Z.match(value):
                bad.append(f"attribution_epoch[{repo!r}] must be RFC 3339 UTC with an explicit Z")

    total, scanned = meta.get("repos_total"), meta.get("repos_scanned")
    failed, stale = meta.get("repos_failed"), meta.get("repos_stale")
    errors = meta.get("repo_errors")
    if isinstance(total, int) and isinstance(scanned, int):
        if min(total, scanned) < 0:
            bad.append("repo counts must be non-negative")
        elif scanned + len(failed or []) != total:
            bad.append(
                "coverage arithmetic: repos_scanned + len(repos_failed) must equal repos_total"
            )
    if isinstance(failed, list) and isinstance(stale, list):
        overlap = set(failed) & set(stale)
        if overlap:
            bad.append(f"repos_stale must be disjoint from repos_failed: {sorted(overlap)}")
    if isinstance(scanned, int) and isinstance(stale, list) and len(stale) > scanned:
        bad.append("repos_stale cannot be larger than the scanned set")
    if isinstance(errors, dict) and isinstance(failed, list):
        if set(errors) != set(failed):
            bad.append("repo_errors keys must be exactly repos_failed")
        oversized = [k for k, v in errors.items() if len(v) > 200]
        if oversized:
            bad.append(f"repo_errors values are truncated to 200 chars: {oversized}")
    with_beads = meta.get("repos_with_bead_data")
    if isinstance(with_beads, int) and isinstance(scanned, int) and with_beads > scanned:
        bad.append("repos_with_bead_data cannot exceed repos_scanned")

    for name in _POSITIVE_INTS:
        value = meta.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value < 1:
            bad.append(f"{name} must be a positive integer")
    cycle_seconds = meta.get("cycle_seconds")
    if isinstance(cycle_seconds, (int, float)) and not isinstance(cycle_seconds, bool) \
            and cycle_seconds < 0:
        bad.append("cycle_seconds must be non-negative")
    cells = meta.get("bulk_bead_cells")
    if isinstance(cells, int) and not isinstance(cells, bool) and cells < 0:
        bad.append("bulk_bead_cells must be non-negative")

    unassigned = meta.get("unassigned_repos")
    if isinstance(unassigned, list) and unassigned != sorted(set(unassigned)):
        bad.append("unassigned_repos must be sorted and unique")
    for name in _REPO_LISTS:
        value = meta.get(name)
        if isinstance(value, list) and len(value) != len(set(value)):
            bad.append(f"{name} must not repeat an entry")
    return bad


# --- fixtures ---------------------------------------------------------------
#
# Three shapes cover the contract's branches: a fully populated cycle, a
# fleet where no repo carried bead data (the null epoch), and an empty
# enumeration (which publishes rather than failing — data-sources.md,
# "Failure semantics").


def _full_fleet():
    return {
        "version": "0.1.10",
        "cycle_id": "20260923T150000Z-1a2b3c4d",
        "generated_at": "2026-09-23T15:00:00Z",
        "window_days": 90,
        "repos_total": 112,
        "repos_scanned": 111,
        "repos_failed": ["another-repo"],
        "repo_errors": {"another-repo": "git clone ... timed out after 600s"},
        "repos_stale": ["one-repo"],
        "mirrors_pruned": ["a-renamed-repo"],
        "repos_with_bead_data": 64,
        "git_timeout_seconds": 600,
        "cycle_seconds": 148.6,
        "bead_epoch_utc": "2026-08-14T16:42:03Z",
        "attribution_epoch": {"gitact-repo": "2026-09-06T05:12:09Z"},
        "bulk_bead_cells": 12,
        "unassigned_repos": ["zeta-tool"],
        "trim_max_lines": 5000,
        "trim_max_files": 200,
        "excluded_path_patterns": [r"(^|/)\.beads/", r"\.(min\.js|min\.css|map)$"],
    }


def _no_bead_data():
    meta = _full_fleet()
    meta["bead_epoch_utc"] = None
    meta["attribution_epoch"] = {}
    meta["repos_with_bead_data"] = 0
    meta["bulk_bead_cells"] = 0
    return meta


def _empty_fleet():
    meta = _full_fleet()
    meta.update({
        "repos_total": 0, "repos_scanned": 0,
        "repos_failed": [], "repo_errors": {},
        "repos_stale": [], "mirrors_pruned": [],
        "repos_with_bead_data": 0, "bead_epoch_utc": None,
        "attribution_epoch": {},
        "bulk_bead_cells": 0, "unassigned_repos": [],
    })
    return meta


def _invalid_fixtures():
    """(label, mutation, expected violation) — each breaks exactly one clause."""
    def drop(name):
        return lambda meta: meta.pop(name)

    def set_(name, value):
        return lambda meta: meta.update({name: value})

    return [
        ("missing required field", drop("generated_at"),
         "missing required field: generated_at"),
        ("unknown field", set_("schema_version", 1),
         "unknown field: schema_version"),
        ("wrong type on a count", set_("repos_total", "112"),
         "repos_total is not of contract type"),
        ("null where never null", set_("repos_failed", None),
         "repos_failed is null and the field is never null"),
        ("naive generated_at", set_("generated_at", "2026-09-23T15:00:00"),
         "generated_at must be RFC 3339 UTC with an explicit Z"),
        ("naive bead_epoch_utc", set_("bead_epoch_utc", "2026-08-14T16:42:03"),
         "bead_epoch_utc must be RFC 3339 UTC with an explicit Z"),
        ("cycle_id names another cycle", set_("cycle_id", "20260901T000000Z-1a2b3c4d"),
         "cycle_id must name the cycle's generated_at"),
        ("repo_errors key not in repos_failed",
         set_("repo_errors", {"another-repo": "x", "ghost-repo": "y"}),
         "repo_errors keys must be exactly repos_failed"),
        ("oversized error message", set_("repo_errors", {"another-repo": "x" * 201}),
         "truncated to 200 chars"),
        ("coverage arithmetic broken", set_("repos_scanned", 110),
         "must equal repos_total"),
        ("failed and stale overlap", set_("repos_stale", ["another-repo"]),
         "disjoint from repos_failed"),
        ("stale exceeds the scanned set",
         set_("repos_stale", [f"repo-{i}" for i in range(112)]),
         "larger than the scanned set"),
        ("bead coverage exceeds scanned", set_("repos_with_bead_data", 200),
         "cannot exceed repos_scanned"),
        ("negative cycle_seconds", set_("cycle_seconds", -1.0),
         "cycle_seconds must be non-negative"),
        ("unsorted unassigned_repos", set_("unassigned_repos", ["zeta-tool", "alpha-tool"]),
         "sorted and unique"),
    ]


# --- the contract against its three sources ---------------------------------


def _documented_section():
    text = OUTPUT_SCHEMA_MD.read_text()
    _, _, section = text.partition("## `meta.json`")
    assert section, "output-schema.md lost the meta.json section"
    return section


def _documented_example():
    fence = re.search(r"```json\n(.*?)```", _documented_section(), re.DOTALL)
    assert fence, "meta.json must keep a JSON example"
    return json.loads(fence.group(1))


def _documented_table_fields():
    _, _, table = _documented_section().partition("| Field |")
    assert table, "meta.json must document its fields in a table"
    return re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE)


def _built_meta(event_ts=(1789327200,), events=None):
    cfg = SimpleNamespace(
        version="test", window_days=90, git_timeout_seconds=600,
        trim_max_lines=5000, trim_max_files=200,
        excluded_path_patterns=[r"(^|/)\.beads/"],
    )
    stats = {
        "repos_total": 2, "repos_scanned": 1, "repos_failed": ["another-repo"],
        "repo_errors": {"another-repo": "timed out"}, "repos_stale": [],
        "mirrors_pruned": [], "repos_with_bead_data": 1, "bulk_bead_cells": 0,
    }
    if events is None:
        events = [{"ts": ts} for ts in event_ts]
    hourly = [{"repo": "some-repo", "family": "unassigned"}]
    return main.build_meta(cfg, stats, "2026-09-23T15:00:00Z", 148.64, events, hourly,
                           cycle_id="20260923T150000Z-1a2b3c4d")


def test_documented_example_validates():
    assert validate_meta(_documented_example()) == []


def test_contract_matches_builder_and_documented_table():
    built = _built_meta()
    assert list(META_CONTRACT) == list(built), "contract order must be build_meta's"
    assert _documented_table_fields() == list(META_CONTRACT), \
        "the field table and the executable contract name the same fields in the same order"


def test_builder_output_validates():
    assert validate_meta(_built_meta()) == []


def test_builder_null_epoch_without_bead_events():
    built = _built_meta(event_ts=())
    assert built["bead_epoch_utc"] is None
    assert built["attribution_epoch"] == {}
    assert validate_meta(built) == []


def test_builder_publishes_the_first_attributed_close_per_repo():
    events = [
        {"ts": 100, "repo": "repo-a", "kind": "closed", "actor": "system"},
        {"ts": 200, "repo": "repo-a", "kind": "closed", "actor": "worker-a"},
        {"ts": 300, "repo": "repo-a", "kind": "closed", "actor": "worker-b"},
        {"ts": 400, "repo": "repo-b", "kind": "closed", "actor": "worker-c"},
    ]

    built = _built_meta(events=events)

    assert built["attribution_epoch"] == {
        "repo-a": "1970-01-01T00:03:20Z",
        "repo-b": "1970-01-01T00:06:40Z",
    }
    assert validate_meta(built) == []


@pytest.mark.parametrize(
    "fixture", [_full_fleet, _no_bead_data, _empty_fleet],
    ids=["full-fleet", "no-bead-data", "empty-fleet"],
)
def test_valid_fixtures_validate(fixture):
    assert validate_meta(fixture()) == []


@pytest.mark.parametrize(
    "label,mutate,expected", _invalid_fixtures(), ids=[c[0] for c in _invalid_fixtures()]
)
def test_invalid_fixtures_are_rejected(label, mutate, expected):
    meta = _full_fleet()
    mutate(meta)
    violations = validate_meta(meta)
    assert any(expected in v for v in violations), f"{label}: {violations}"
