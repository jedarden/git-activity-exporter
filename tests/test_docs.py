import re
from pathlib import Path

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
