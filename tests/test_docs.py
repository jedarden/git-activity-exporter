import json
import re
from pathlib import Path
from types import SimpleNamespace

from src import families, main
from src.config import DEFAULT_EXCLUDED_PATHS

CONFIGURATION_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "configuration.md"
)
OUTPUT_SCHEMA_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "output-schema.md"
)
DATA_SOURCES_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "data-sources.md"
)
DEPLOYMENT_MD = (
    Path(__file__).resolve().parent.parent / "docs" / "notes" / "deployment.md"
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


def test_commit_bulk_contract_is_documented_at_both_surfaces():
    configuration = " ".join(CONFIGURATION_MD.read_text().split())
    schema = " ".join(OUTPUT_SCHEMA_MD.read_text().split())

    for phrase in (
        "independent, strict upper bounds",
        "Equality is not bulk",
        "The flag is an annotation, not a deletion",
        "Those raw line fields never trigger `is_bulk`",
    ):
        assert phrase in configuration, f"commit bulk contract missing from configuration.md: {phrase}"

    for phrase in (
        "`is_bulk` is a per-commit annotation",
        "does not create a second bulk row or a bulk/non-bulk split",
        "Both tests are independent, and both are strict",
        "full `lines_added_raw` and `lines_deleted_raw` values",
    ):
        assert phrase in schema, f"commit bulk contract missing from output-schema.md: {phrase}"


def test_legacy_fixed_key_consistency_contract_is_documented():
    schema = " ".join(OUTPUT_SCHEMA_MD.read_text().split())

    for phrase in (
        "validate it against the pointer before returning any of its bytes",
        "byte-for-byte equal to its named immutable body",
        "retries the complete sequence at most three times",
        "discards every value from that attempt",
        "the read is rejected with no data returned",
        "data-before-`meta.json` window",
    ):
        assert phrase in schema, f"legacy fixed-key consistency contract missing: {phrase}"


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


def test_forgejo_git_credential_contract_is_documented():
    text = DATA_SOURCES_MD.read_text()
    _, _, section = text.partition("### Forgejo Git credentials")
    assert section, "data-sources.md lost the Forgejo Git credential section"
    section = section.split("\n### ", 1)[0]
    section = " ".join(section.split())

    for phrase in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0=credential.helper",
        "GIT_CONFIG_VALUE_0",
        "GIT_TERMINAL_PROMPT=0",
        "command argument",
        "Git stderr is captured",
        "Authentication failures are classified as non-transient",
        "existing mirror is kept",
    ):
        assert phrase in section, f"credential-safety contract missing: {phrase}"


def test_release_reproducibility_update_procedure_is_documented():
    text = " ".join(DEPLOYMENT_MD.read_text().split())
    _, _, section = text.partition("### Updating reproducibility pins")
    assert section, "deployment.md lost the reproducibility update procedure"
    for phrase in (
        "requirements.txt",
        "requirements-dev.txt",
        "exact `package==version` pin",
        "docker buildx imagetools inspect",
        "64-character SHA-256 digest",
        "python scripts/check-release-drift.py",
        "docker build --tag local/git-activity-pin-review:local .",
    ):
        assert phrase in section, f"reproducibility procedure missing: {phrase}"


def test_runtime_and_release_forgejo_credential_roles_are_documented():
    text = " ".join(DEPLOYMENT_MD.read_text().split())
    _, _, section = text.partition("### Forgejo credential roles")
    assert section, "deployment.md lost the Forgejo credential-roles section"
    section = section.split("### ", 1)[0]
    for phrase in (
        "FORGE_TOKEN",
        "git-activity-exporter-forge",
        "read:repository",
        "read-only repository scope",
        "FORGEJO_TOKEN",
        "forgejo-webhook-token",
        "write:repository",
        "runtime exporter is therefore read-only",
        "complete workflow and workload manifests",
        "never referenced by the runtime Deployment",
        "secretKeyRef",
    ):
        assert phrase in section, f"Forgejo credential-role contract missing: {phrase}"


def test_mirror_capacity_and_exhaustion_contract_is_documented():
    deployment = " ".join(DEPLOYMENT_MD.read_text().split())
    self_hosting = " ".join(
        (DEPLOYMENT_MD.parent.parent / "self-hosting.md").read_text().split()
    )
    monitoring = (
        DEPLOYMENT_MD.parent.parent.parent
        / "examples"
        / "self-hosting"
        / "monitoring.yaml"
    ).read_text()

    for phrase in (
        "20Gi",
        "14.04 GiB",
        "largest mirror was 3.4 GiB",
        "at least 20% free",
        "temporary pack space",
        "ENOSPC",
        "preserves an existing mirror",
        "publishes nothing",
        "Mirror volume capacity runbook",
    ):
        assert phrase in deployment, f"mirror capacity contract missing from deployment.md: {phrase}"
    for phrase in (
        "30 GiB or more",
        "kubelet_volume_stats_*",
        "less than 20% free space",
        "less than 10% or 2 GiB free",
        "removes any failed",
        "fails the cycle before publication",
    ):
        assert phrase in self_hosting, f"mirror capacity contract missing from self-hosting.md: {phrase}"
    for phrase in (
        "GitActivityExporterMirrorVolumeLowSpace",
        "GitActivityExporterMirrorVolumeCritical",
        "GitActivityExporterMirrorVolumeMetricsMissing",
        "kubelet_volume_stats_available_bytes",
        "kubelet_volume_stats_capacity_bytes",
    ):
        assert phrase in monitoring, f"mirror capacity alert missing: {phrase}"


def test_docker_hub_registry_credential_contract_is_documented():
    text = " ".join(DEPLOYMENT_MD.read_text().split())
    _, _, section = text.partition("### Docker Hub registry credential contract")
    assert section, "deployment.md lost the Docker Hub registry credential contract"
    section = section.split("### ", 1)[0]
    for phrase in (
        "docker-hub-registry",
        "argo-workflows",
        "kubernetes.io/dockerconfigjson",
        ".dockerconfigjson",
        "/kaniko/.docker/config.json",
        "ronaldraygun/git-activity-exporter",
        "ronaldraygun/cache",
        "Read & Write",
        "rs-manager/iad-ci/docker/build",
        "force-sync",
        "SecretSynced=True",
        "continueOn",
        "before `promote`",
        "does not expose the PAT",
    ):
        assert phrase in section, f"Docker Hub credential contract missing: {phrase}"


def test_plan_and_self_hosting_scope_separate_runtime_reads_from_release_writes():
    plan = " ".join(
        (DEPLOYMENT_MD.parent.parent / "plan" / "plan.md").read_text().split()
    )
    self_hosting = " ".join(
        (DEPLOYMENT_MD.parent.parent / "self-hosting.md").read_text().split()
    )

    for phrase in (
        "runtime exporter is read-only with respect to source repositories",
        "separate, privileged",
        "automatic `VERSION` write-back",
        "does write its published dataset to S3",
    ):
        assert phrase in plan, f"plan scope boundary missing: {phrase}"

    for phrase in (
        "runtime read-only credential",
        "read:repository",
        "never reuse `FORGE_TOKEN`",
        "automatic `VERSION` write-back",
    ):
        assert phrase in self_hosting, f"self-hosting credential boundary missing: {phrase}"


def test_s3_credential_provisioning_and_redaction_contract_is_documented():
    text = CONFIGURATION_MD.read_text()
    _, _, section = text.partition("## Destination credentials (`DEST_S3_*`)")
    assert section, "configuration.md lost the S3 credential section"
    section = section.split("\n## ", 1)[0]
    for phrase in (
        "process environment",
        "does not accept S3 credentials as command-line flags",
        "never passes them to a subprocess",
        "ephemeral `--env-file`",
        "client-construction errors",
        "publication errors",
        "`/health` response",
    ):
        assert phrase in section, f"S3 credential-safety contract missing: {phrase}"


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
