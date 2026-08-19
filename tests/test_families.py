import pytest

from src import families


def test_duplicate_repo_across_families_is_an_error(tmp_path):
    # Silent last-write-wins would make family totals depend on dict ordering.
    p = tmp_path / "f.yaml"
    p.write_text("families:\n  a:\n    - repo1\n  b:\n    - repo1\n")
    with pytest.raises(ValueError, match="repo1"):
        families.load(str(p))


def test_missing_file_degrades_instead_of_crashing(tmp_path):
    mapping = families.load(str(tmp_path / "nope.yaml"))
    assert mapping == {}
    assert families.family_of(mapping, "anything") == families.UNASSIGNED


def test_shipped_families_file_is_valid():
    mapping = families.load("families.yaml")
    assert mapping["commitgraph"] == mapping["commitgraph-deprecated"] == "commitgraph"
    assert mapping["NEEDLE"] == "agent-fleet"
