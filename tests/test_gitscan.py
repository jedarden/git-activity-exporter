import re

from src import gitscan
from src.config import DEFAULT_EXCLUDED_PATHS


def test_bead_id_prefers_trailer_over_scope():
    assert gitscan._bead_id_from("fix(needle-aaaaaaaa): x", "needle-bbbbbbbb") == "needle-bbbbbbbb"


def test_bead_id_from_conventional_scope():
    assert gitscan._bead_id_from("fix(needle-318c33ba): thing", "") == "needle-318c33ba"


def test_bead_id_ignores_non_bead_scopes():
    # A scope that is a component name, not a bead id, must not be recorded as
    # one -- `fix(span):` is real and appears in NEEDLE's history.
    assert gitscan._bead_id_from("fix(span): resolve type mismatch", "") is None
    assert gitscan._bead_id_from("docs: no scope at all", "") is None


def test_default_exclusions_catch_the_dominant_contaminator():
    # .beads/ churn was 68.1% of all line volume across 105 repos over 30
    # days; if this stops matching, "lines of code" silently becomes
    # "checkpoint bookkeeping".
    pats = [re.compile(p) for p in DEFAULT_EXCLUDED_PATHS]

    def excluded(path):
        return any(p.search(path) for p in pats)

    assert excluded(".beads/checkpoint/forensic.jsonl")
    assert excluded("sub/.beads/checkpoint/objects/gen-abc.jsonl")
    assert excluded("web/node_modules/left-pad/index.js")
    assert excluded("Cargo.lock")
    assert excluded("crates/core/Cargo.lock")
    assert excluded("public/vendor/hyparquet/index.js")
    assert excluded("app/bundle.min.js")
    assert not excluded("src/main.rs")
    assert not excluded("docs/plan/plan.md")
    # A path merely containing the word must not be swept up.
    assert not excluded("src/beads_client.rs")


def test_mark_bulk_flags_but_does_not_drop():
    commits = [
        {"lines_added": 10, "lines_deleted": 5, "files_changed": 3},
        {"lines_added": 9_000_000, "lines_deleted": 0, "files_changed": 133},
        {"lines_added": 12, "lines_deleted": 0, "files_changed": 25_639},
    ]
    out = gitscan.mark_bulk(commits, 5000, 200)
    assert [c["is_bulk"] for c in out] == [False, True, True]
    assert len(out) == 3, "bulk commits must survive as commits"


def test_credentials_never_survive_into_a_log_line():
    # The first live cold pass leaked the Forgejo token into the pod log:
    # subprocess.TimeoutExpired stringifies the whole argv, and the token was
    # embedded in the clone URL. Both halves of the fix are asserted here --
    # argv no longer carries it, and any that reappears is scrubbed anyway.
    leaky = "https://x-access-token:DEADBEEFCAFE1234@git.ardenone.com/jedarden/x.git"
    assert "DEADBEEFCAFE1234" not in gitscan._scrub(leaky)
    assert gitscan._scrub(leaky) == "https://<redacted>@git.ardenone.com/jedarden/x.git"

    rendered = gitscan._safe(["git", "clone", "--mirror", leaky, "/data/mirrors/x.git.tmp"])
    assert "DEADBEEF" not in rendered
    assert "<redacted>" in rendered

    # stderr from git is scrubbed on the same path
    assert "DEADBEEF" not in gitscan._scrub(f"fatal: could not read from {leaky}")


def test_token_travels_in_env_not_argv():
    env = gitscan._credential_env("SUPERSECRET")
    # Present for git to consume...
    assert env["FORGE_TOKEN"] == "SUPERSECRET"
    # ...but only ever dereferenced by name, so it cannot appear in a command
    # line, a process listing, or an exception string.
    assert "SUPERSECRET" not in env["GIT_CONFIG_VALUE_0"]
    assert "$FORGE_TOKEN" in env["GIT_CONFIG_VALUE_0"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
