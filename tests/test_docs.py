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
