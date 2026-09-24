import json
import re
from pathlib import Path
from types import SimpleNamespace

from src import families, main
from src.config import DEFAULT_EXCLUDED_PATHS

CONFIGURATION_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "configuration.md"
)


def _documented_exclusions():
    text = CONFIGURATION_MD.read_text()
    _, _, section = text.partition("## Default excluded-path patterns")
    assert section, "configuration.md lost the default-patterns section"
    fence = re.search(r"```\n(.*?)```", section, re.DOTALL)
    assert fence, "default patterns must be enumerated in a fenced block"
    return [line for line in fence.group(1).splitlines() if line.strip()]


def test_documented_exclusions_match_code():
    # The README sells the LOC filter as auditable via lines_*_raw, which only
    # holds if the pattern list is readable from the docs. Either side edited
    # without the other fails here instead of drifting silently.
    assert _documented_exclusions() == list(DEFAULT_EXCLUDED_PATHS)


def test_dest_s3_variables_are_documented():
    # The README tells reusers to bring DEST_S3_* credentials, but the exact
    # variable names exist only as _require/_optional calls in config.py.
    # Every name the code reads must appear in configuration.md, or a reuser
    # cannot deploy from the documentation alone.
    config_py = Path(__file__).resolve().parent.parent / "src" / "config.py"
    names = sorted(set(re.findall(r'"(DEST_S3_[A-Z_]+)"', config_py.read_text())))
    assert names, "no DEST_S3_* variables found in src/config.py"
    text = CONFIGURATION_MD.read_text()
    for name in names:
        assert f"`{name}`" in text, f"{name} missing from docs/notes/configuration.md"


def test_documented_meta_keys_match_builder():
    section = CONFIGURATION_MD.parent / "output-schema.md"
    text = section.read_text()
    _, _, meta_section = text.partition("## `meta.json`")
    assert meta_section, "output-schema.md lost the meta.json section"
    fence = re.search(r"```json\n(.*?)```", meta_section, re.DOTALL)
    assert fence, "meta.json must have a JSON example"
    documented = json.loads(fence.group(1))

    cfg = SimpleNamespace(
        version="test",
        window_days=90,
        git_timeout_seconds=600,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=[],
    )
    stats = {
        "repos_total": 1,
        "repos_scanned": 1,
        "repos_failed": [],
        "repo_errors": {},
        "repos_stale": [],
        "repos_partial_history": [],
        "mirrors_pruned": [],
        "repos_with_bead_data": 0,
        "bulk_bead_cells": 0,
    }
    built = main.build_meta(cfg, stats, "2026-09-16T00:00:00Z", 1.0, [], [],
                            cycle_id="20260916T000000Z-01234567")
    assert set(built) == set(documented)


def test_documented_families_example_loads(tmp_path):
    # The yaml example in configuration.md's families section is the schema
    # reusers copy. It must be a document families.load actually accepts --
    # either side edited without the other fails here, the way the
    # exclusion-pattern block is held against DEFAULT_EXCLUDED_PATHS.
    text = CONFIGURATION_MD.read_text()
    _, _, section = text.partition("## The families file")
    assert section, "configuration.md lost the families-file section"
    fence = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert fence, "the families file schema must be given as a yaml example"
    p = tmp_path / "documented.yaml"
    p.write_text(fence.group(1))

    mapping = families.load(str(p))

    assert mapping["NEEDLE"] == "agent-fleet"
    assert mapping["declarative-config"] == "infra"


def test_family_attribution_over_time_is_pinned():
    # The temporal decision (per-publication, never retroactive) is the part
    # of the families contract with no test of its own -- it lives in prose.
    # Losing the section, or the pin itself, must fail rather than drift.
    path = CONFIGURATION_MD.parent / "output-schema.md"
    text = path.read_text()
    _, _, section = text.partition("## Family attribution over time")
    assert section, "output-schema.md lost the family-attribution-over-time section"
    section = section.split("\n## ", 1)[0]
    assert "never retroactive" in section
