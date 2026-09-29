"""Contract tests for the versioned Argo release path.

The manifests are owned by the sibling ``declarative-config`` repository.
When that checkout is available, these tests inspect its manifests directly;
the small committed fixtures keep the test suite reproducible from an
application-only archive as well.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
FAKE_RELEASE_SECRETS = {
    "FORGEJO_TOKEN": "forgejo-token-redaction-test-only",
    "GIT_AUTH_TOKEN": "git-auth-token-redaction-test-only",
    "DOCKER_CONFIG": "docker-config-redaction-test-only",
}
SECRET_ENV_NAMES = frozenset(FAKE_RELEASE_SECRETS)
WORKFLOW_RELATIVE = Path(
    "k8s/iad-ci/argo-workflows/git-activity-exporter-build-workflowtemplate.yml"
)
SENSOR_RELATIVE = Path(
    "k8s/iad-ci/argo-events/git-activity-exporter-sensor.yml"
)


def _manifest(relative_path: Path, fixture_name: str) -> dict:
    sibling = ROOT.parent / "declarative-config" / relative_path
    path = sibling if sibling.is_file() else FIXTURES / fixture_name
    return yaml.safe_load(path.read_text())


def _workflow() -> dict:
    return _manifest(WORKFLOW_RELATIVE, "git-activity-exporter-workflow.yml")


def _fixture_workflow() -> dict:
    return yaml.safe_load(
        (FIXTURES / "git-activity-exporter-workflow.yml").read_text()
    )


def _sensor() -> dict:
    return _manifest(SENSOR_RELATIVE, "git-activity-exporter-sensor.yml")


def _deployment_resources() -> list[dict]:
    sibling_dir = ROOT.parent / "declarative-config" / (
        "k8s/ardenone-cluster/git-activity-exporter"
    )
    sibling = sibling_dir / "deployment.yml"
    if sibling.is_file():
        paths = [sibling, sibling_dir / "resourcequota.yml"]
    else:
        paths = [ROOT / "examples" / "self-hosting" / "kubernetes.yaml"]
    resources = []
    for path in paths:
        if path.is_file():
            resources.extend(
                resource for resource in yaml.safe_load_all(path.read_text()) if resource
            )
    return resources


def _image_fields(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "image" and isinstance(nested, str):
                yield nested
            yield from _image_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _image_fields(nested)


def _assert_explicit_image_pin(image: str) -> None:
    image_without_digest, separator, digest = image.partition("@")
    if separator:
        assert digest.startswith("sha256:"), image
        assert digest.removeprefix("sha256:"), image
        return
    last_component = image_without_digest.rsplit("/", 1)[-1]
    assert ":" in last_component, f"image has no tag: {image}"
    assert last_component.rsplit(":", 1)[1].lower() != "latest", image


def _templates(workflow: dict) -> dict[str, dict]:
    return {template["name"]: template for template in workflow["spec"]["templates"]}


def _env_by_name(template: dict) -> dict[str, dict]:
    env = template.get("env")
    if env is None and isinstance(template.get("script"), dict):
        env = template["script"].get("env", [])
    return {entry["name"]: entry for entry in env or []}


def _secret_references(value):
    """Yield every Secret reference in a manifest, including nested pods."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"secretKeyRef", "secretRef"} and isinstance(nested, dict):
                yield nested
            else:
                yield from _secret_references(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _secret_references(nested)


def _parameter_values(resource: dict) -> dict[str, str]:
    return {
        parameter["name"]: parameter["value"]
        for parameter in resource["spec"]["arguments"]["parameters"]
    }


def _assert_origin_only(source: str) -> None:
    """Ensure release scripts cannot select a second or GitHub push remote."""
    assert "github.com" not in source.lower()
    assert not re.search(r"(?m)^\s*git\s+remote\b", source)

    for operation in ("fetch", "pull", "push"):
        for match in re.finditer(
            rf"\bgit\s+{operation}\b([^\n;&|]*)", source
        ):
            arguments = shlex.split(match.group(1))
            assert "--all" not in arguments
            assert "--mirror" not in arguments
            remotes = [
                argument for argument in arguments if not argument.startswith("-")
            ]
            if remotes:
                assert remotes[0] == "origin", match.group(0)


def _render_workflow_parameters(value: str) -> str:
    """Render non-secret Argo values before exercising shell command lines."""
    replacements = {
        "{{workflow.parameters.git-repo}}": "jedarden/git-activity-exporter",
        "{{workflow.parameters.branch}}": "main",
        "{{workflow.parameters.commit-sha}}": "a" * 40,
        "{{inputs.parameters.version}}": "1.2.4",
        "{{inputs.parameters.source-sha}}": "b" * 40,
        "{{inputs.parameters.verified-image}}": (
            "ronaldraygun/git-activity-exporter:1.2.4"
        ),
        "{{inputs.parameters.gitops-revision}}": "c" * 40,
        "{{steps.resolve-version.outputs.parameters.version}}": "1.2.4",
        "{{steps.resolve-version.outputs.parameters.source-sha}}": "b" * 40,
        "{{steps.smoke.outputs.parameters.verified-image}}": (
            "ronaldraygun/git-activity-exporter:1.2.4"
        ),
        "{{steps.promote.outputs.parameters.gitops-revision}}": "c" * 40,
    }
    for placeholder, replacement in replacements.items():
        value = value.replace(placeholder, replacement)
    return value


def _joined_shell_lines(source: str) -> list[str]:
    """Join shell continuation lines so command arguments can be captured."""
    joined = []
    pending = ""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if pending:
            line = f"{pending}{line}"
        if line.endswith("\\"):
            pending = f"{line[:-1]} "
        else:
            joined.append(line)
            pending = ""
    if pending:
        joined.append(pending)
    return joined


def _release_git_commands(source: str) -> list[tuple[str, str]]:
    """Extract clone/fetch/push commands for execution by a fake Git binary."""
    commands = []
    for line in _joined_shell_lines(_render_workflow_parameters(source)):
        match = re.search(r"\bgit\s+(clone|fetch|pull|push)\b.*", line)
        if not match:
            continue
        command = re.split(r"\s*(?:;|&&|\|\|)\s*", match.group(0), maxsplit=1)[
            0
        ].strip()
        commands.append((match.group(1), command))
    return commands


def _assert_no_fake_secret(artifact: str, label: str) -> None:
    """Keep fake credentials out of an artifact without echoing their values."""
    for name, value in FAKE_RELEASE_SECRETS.items():
        assert value not in artifact, f"{label} leaked {name}"


def _source_without_credential_helper(template: dict) -> str:
    source = template.get("script", {}).get("source", "")
    for entry in _env_by_name(template).values():
        if entry.get("name") == "GIT_CONFIG_VALUE_0":
            source = source.replace(entry.get("value", ""), "")
    return source


def _assert_secret_outputs_are_not_printed(template: dict) -> None:
    """Reject shell logging/tracing that could expose an injected secret."""
    source = _source_without_credential_helper(template)
    secret_names = "|".join(re.escape(name) for name in SECRET_ENV_NAMES)
    assert not re.search(r"(?m)^\s*set\s+-[^\n]*x", source)
    assert not re.search(r"(?im)^\s*(?:printenv|env)\b", source)
    assert not re.search(
        rf"(?im)^\s*(?:echo|printf|printenv|env|cat|tee|logger)\b[^\n]*"
        rf"(?:\$(?:\{{)?(?:{secret_names})\}}?|\b(?:{secret_names})\b)",
        source,
    )
    for name in SECRET_ENV_NAMES:
        assert f"${name}" not in source
        assert f"${{{name}}}" not in source


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _new_bare_repository(root: Path, name: str) -> tuple[Path, Path]:
    origin = root / f"{name}.git"
    seed = root / f"{name}-seed"
    _git(root, "init", "--bare", "--initial-branch=main", str(origin))
    _git(root, "init", "--initial-branch=main", str(seed))
    _git(seed, "config", "user.name", "Release test")
    _git(seed, "config", "user.email", "release-test@example.invalid")
    _git(seed, "remote", "add", "origin", str(origin))
    return origin, seed


def _application_origin(root: Path, *, explicit_version_change: bool) -> Path:
    origin, seed = _new_bare_repository(root, "application")
    (seed / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (seed / "release-input.txt").write_text("initial\n", encoding="utf-8")
    _git(seed, "add", "VERSION", "release-input.txt")
    _git(seed, "commit", "-m", "initial release state")

    if explicit_version_change:
        (seed / "VERSION").write_text("1.2.9\n", encoding="utf-8")
    else:
        (seed / "release-input.txt").write_text("application change\n", encoding="utf-8")
    _git(seed, "add", "VERSION", "release-input.txt")
    _git(seed, "commit", "-m", "trigger release")
    _git(seed, "push", "--set-upstream", "origin", "main")
    return origin


def _run_resolve_version(
    workflow: dict,
    root: Path,
    origin: Path,
    expected_version_path: Path,
    *,
    commit_sha: str | None = None,
    expect_failure: bool = False,
) -> str | subprocess.CompletedProcess[str]:
    """Run the WorkflowTemplate's resolver against a local bare Git repo."""
    script = _templates(workflow)["resolve-version"]["script"]["source"]
    checkout = root / f"resolve-checkout-{expected_version_path.name}"
    commit_sha = commit_sha or _git(
        root, "--git-dir", str(origin), "rev-parse", "main"
    )
    clone_pattern = re.compile(
        r'git clone --branch \{\{workflow\.parameters\.branch\}\} \\\n'
        r'\s+"https://git\.ardenone\.com/\{\{workflow\.parameters\.git-repo\}\}\.git" \\\n'
        r'\s+/tmp/repo'
    )
    script, replacements = clone_pattern.subn(
        "git clone --branch main "
        f"{shlex.quote(str(origin))} {shlex.quote(str(checkout))}",
        script,
        count=1,
    )
    assert replacements == 1, script
    script = script.replace("/tmp/repo", str(checkout))
    script = script.replace("/tmp/version", str(expected_version_path))
    script = script.replace("/tmp/source-sha", str(expected_version_path) + ".sha")
    script = script.replace("{{workflow.parameters.commit-sha}}", commit_sha)
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if expect_failure:
        assert result.returncode != 0
        return result
    assert result.returncode == 0, result.stderr
    return expected_version_path.read_text(encoding="utf-8").strip()


def _gitops_repository(root: Path) -> Path:
    origin, seed = _new_bare_repository(root, "declarative-config")
    manifest = seed / "k8s/ardenone-cluster/git-activity-exporter/deployment.yml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "spec:\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: exporter\n"
        "          image: ronaldraygun/git-activity-exporter:1.2.3\n",
        encoding="utf-8",
    )
    _git(seed, "add", str(manifest.relative_to(seed)))
    _git(seed, "commit", "-m", "initial GitOps state")
    _git(seed, "push", "--set-upstream", "origin", "main")
    return origin


def _run_promotion(
    workflow: dict,
    root: Path,
    gitops_origin: Path,
    version: str,
    *,
    attempt: str = "first",
    verified_image: str | None = None,
) -> tuple[str, str]:
    """Run the promotion script locally and return its revision and image."""
    script = _templates(workflow)["promote"]["script"]["source"]
    checkout = root / f"gitops-checkout-{attempt}"
    clone_pattern = re.compile(
        r'git clone --branch main --depth 1 \\\n'
        r'\s+"https://git\.ardenone\.com/jedarden/declarative-config\.git" \\\n'
        r'\s+/tmp/declarative-config'
    )
    script, replacements = clone_pattern.subn(
        "git clone --branch main --depth 1 "
        f"{shlex.quote(str(gitops_origin))} {shlex.quote(str(checkout))}",
        script,
        count=1,
    )
    assert replacements == 1, script
    script = script.replace("/tmp/declarative-config", str(checkout))
    script = script.replace(
        'VERSION="{{inputs.parameters.version}}"', f'VERSION="{version}"'
    )
    expected_image = f"ronaldraygun/git-activity-exporter:{version}"
    if 'VERIFIED_IMAGE="{{inputs.parameters.verified-image}}"' in script:
        script = script.replace(
            'VERIFIED_IMAGE="{{inputs.parameters.verified-image}}"',
            f'VERIFIED_IMAGE="{verified_image or expected_image}"',
        )
    revision_path = root / f"gitops-revision-{attempt}"
    script = script.replace("/tmp/gitops-revision", str(revision_path))
    result = subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    revision = revision_path.read_text(encoding="utf-8").strip()
    promoted_manifest = _git(
        root,
        "--git-dir",
        str(gitops_origin),
        "show",
        f"{revision}:k8s/ardenone-cluster/git-activity-exporter/deployment.yml",
    )
    image = re.search(
        r"^\s+image:\s+(ronaldraygun/git-activity-exporter:\S+)\s*$",
        promoted_manifest,
        re.MULTILINE,
    )
    assert image, promoted_manifest
    return revision, image.group(1)


def _run_promotion_expect_failure(
    workflow: dict,
    root: Path,
    gitops_origin: Path,
    version: str,
    verified_image: str,
) -> subprocess.CompletedProcess[str]:
    """Run promotion with an intentionally unverified image reference."""
    script = _templates(workflow)["promote"]["script"]["source"]
    checkout = root / "gitops-checkout-unverified"
    clone_pattern = re.compile(
        r'git clone --branch main --depth 1 \\\n'
        r'\s+"https://git\.ardenone\.com/jedarden/declarative-config\.git" \\\n'
        r'\s+/tmp/declarative-config'
    )
    script, replacements = clone_pattern.subn(
        "git clone --branch main --depth 1 "
        f"{shlex.quote(str(gitops_origin))} {shlex.quote(str(checkout))}",
        script,
        count=1,
    )
    assert replacements == 1, script
    script = script.replace("/tmp/declarative-config", str(checkout))
    script = script.replace(
        'VERSION="{{inputs.parameters.version}}"', f'VERSION="{version}"'
    )
    script = script.replace(
        'VERIFIED_IMAGE="{{inputs.parameters.verified-image}}"',
        f'VERIFIED_IMAGE="{verified_image}"',
    )
    return subprocess.run(
        ["sh", "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )


def test_failed_tests_cannot_reach_the_version_bump():
    workflow = _workflow()
    templates = _templates(workflow)
    build_steps = workflow["spec"]["templates"][0]["steps"]

    assert workflow["spec"]["entrypoint"] == "build"
    assert [group[0]["name"] for group in build_steps] == [
        "test",
        "resolve-version",
        "docker-build",
        "self-hosting-smoke",
        "smoke",
        "promote",
        "verify-rollout",
    ]

    test = templates["test"]
    test_script = test["script"]["source"]
    assert "set -e" in test_script
    assert "python -m pytest tests/ -q" in test_script
    assert "continueOn" not in test
    assert test["retryStrategy"]["retryPolicy"] == "OnError"

    resolve_script = templates["resolve-version"]["script"]["source"]
    assert "git add VERSION" in resolve_script
    assert 'git config user.name "Argo Workflows CI"' in resolve_script
    assert build_steps[1][0]["template"] == "resolve-version"
    assert build_steps[5][0]["template"] == "promote"
    assert build_steps[6][0]["template"] == "verify-rollout"


def test_self_hosting_smoke_runs_as_a_pinned_image_release_gate():
    workflow = _workflow()
    templates = _templates(workflow)
    smoke_step = workflow["spec"]["templates"][0]["steps"][3][0]
    smoke = templates["self-hosting-smoke"]
    source = smoke["container"]["args"][0]

    assert smoke_step["template"] == "self-hosting-smoke"
    assert smoke_step["arguments"]["parameters"] == [
        {
            "name": "version",
            "value": "{{steps.resolve-version.outputs.parameters.version}}",
        },
        {
            "name": "source-sha",
            "value": "{{steps.resolve-version.outputs.parameters.source-sha}}",
        },
    ]
    assert smoke["container"]["image"] == "docker:29.7.2-dind"
    assert smoke["securityContext"]["privileged"] is True
    assert smoke["retryStrategy"]["retryPolicy"] == "OnError"
    for phrase in (
        "docker compose version",
        "docker pull \"$IMAGE\"",
        "SKIP_BUILD=1 IMAGE=\"$IMAGE\" SMOKE_PYTHON_IMAGE=\"$IMAGE\"",
        "scripts/smoke-self-hosting.sh",
    ):
        assert phrase in source


def test_release_workflow_uses_forgejo_main_repositories_and_origin_only():
    workflow = _workflow()
    templates = _templates(workflow)
    assert _parameter_values(workflow) == {
        "git-repo": "jedarden/git-activity-exporter",
        "branch": "main",
        "commit-sha": "",
    }

    sensor = _sensor()
    trigger_resource = sensor["spec"]["triggers"][0]["template"]["argoWorkflow"][
        "source"
    ]["resource"]
    assert _parameter_values(trigger_resource) == {
        "git-repo": "jedarden/git-activity-exporter",
        "branch": "main",
        "commit-sha": "",
    }

    test_source = templates["test"]["script"]["source"]
    resolve_source = templates["resolve-version"]["script"]["source"]
    promote_source = templates["promote"]["script"]["source"]
    build_source = templates["docker-build"]["container"]["args"][0]

    for source in (test_source, resolve_source, promote_source):
        _assert_origin_only(source)

    assert '"https://git.ardenone.com/{{workflow.parameters.git-repo}}.git"' in (
        test_source
    )
    assert '"https://git.ardenone.com/{{workflow.parameters.git-repo}}.git"' in (
        resolve_source
    )
    assert (
        '"https://git.ardenone.com/jedarden/declarative-config.git"'
        in promote_source
    )
    assert "git clone --branch main" in promote_source
    assert "git fetch origin main" in promote_source
    assert "git push origin HEAD:main" in promote_source
    assert re.search(r"\bgit\s+push\b", resolve_source)
    assert (
        'CONTEXT="https://git.ardenone.com/{{workflow.parameters.git-repo}}.git#'
        '{{inputs.parameters.source-sha}}"'
    ) in build_source


def test_release_fixture_captures_event_sha_and_rejects_stale_main():
    workflow = _fixture_workflow()
    sensor = yaml.safe_load(
        (FIXTURES / "git-activity-exporter-sensor.yml").read_text()
    )
    workflow_parameters = _parameter_values(workflow)
    assert workflow_parameters["commit-sha"] == ""

    trigger = sensor["spec"]["triggers"][0]["template"]
    trigger_resource = trigger["argoWorkflow"]["source"]["resource"]
    assert _parameter_values(trigger_resource)["commit-sha"] == ""
    assert trigger["parameters"] == [
        {
            "src": {
                "dependencyName": "git-activity-exporter-push",
                "dataKey": "body.after",
            },
            "dest": "spec.arguments.parameters.2.value",
        }
    ]

    templates = _templates(workflow)
    test_source = templates["test"]["script"]["source"]
    resolve_source = templates["resolve-version"]["script"]["source"]
    smoke_source = templates["self-hosting-smoke"]["container"]["args"][0]
    build_source = templates["docker-build"]["container"]["args"][0]
    for source in (test_source, resolve_source, smoke_source):
        assert "git checkout --detach" in source
        assert 'test "$(git rev-parse HEAD)" =' in source
    assert "#{{inputs.parameters.source-sha}}" in build_source
    assert "git pull --rebase" not in resolve_source
    assert "refusing stale release" in resolve_source


def test_queued_release_fails_when_main_advanced_past_trigger(tmp_path):
    workflow = _fixture_workflow()
    application_origin = _application_origin(tmp_path, explicit_version_change=False)
    trigger_sha = _git(
        tmp_path, "--git-dir", str(application_origin), "rev-parse", "main"
    )

    seed = tmp_path / "application-seed"
    (seed / "release-input.txt").write_text("newer queued change\n", encoding="utf-8")
    _git(seed, "add", "release-input.txt")
    _git(seed, "commit", "-m", "advance main while release is queued")
    _git(seed, "push", "origin", "main")
    advanced_sha = _git(
        tmp_path, "--git-dir", str(application_origin), "rev-parse", "main"
    )
    assert advanced_sha != trigger_sha

    result = _run_resolve_version(
        workflow,
        tmp_path,
        application_origin,
        tmp_path / "stale-version",
        commit_sha=trigger_sha,
        expect_failure=True,
    )

    assert "refusing stale release" in result.stderr
    assert _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "log",
        "--format=%s",
        "main",
    ).splitlines()[0] == "advance main while release is queued"


def test_promotion_is_serialized_and_verifies_the_pushed_gitops_revision():
    workflow = _workflow()
    templates = _templates(workflow)
    assert workflow["spec"]["synchronization"]["mutexes"] == [
        {"name": "git-activity-exporter-release"}
    ]

    promote = templates["promote"]
    promote_source = promote["script"]["source"]
    assert "declarative-config.git" in promote_source
    assert "k8s/ardenone-cluster/git-activity-exporter/deployment.yml" in promote_source
    assert 'git add "$MANIFEST"' in promote_source
    assert "git commit -m" in promote_source
    assert "git push origin HEAD:main" in promote_source
    assert "/tmp/gitops-revision" in promote_source
    assert promote["outputs"]["parameters"][0]["name"] == "gitops-revision"

    verify = templates["verify-rollout"]["script"]["source"]
    assert "argocd-ro-ardenone-manager-ts.ardenone.com" in verify
    assert "ardenone-cluster-traefik:8001" in verify
    assert "--server=http://ardenone-cluster-traefik:8001" in verify
    assert "kubectl apply" not in verify


def test_resolved_version_drives_both_embedded_version_and_image_tag():
    workflow = _workflow()
    templates = _templates(workflow)
    build_steps = templates["build"]["steps"]
    resolve_output = build_steps[2][0]["arguments"]["parameters"][0]["value"]
    docker = templates["docker-build"]
    build_source = docker["container"]["args"][0]

    assert resolve_output == "{{steps.resolve-version.outputs.parameters.version}}"
    assert (
        "type=image,name=ronaldraygun/git-activity-exporter:"
        "{{inputs.parameters.version}},push=true"
    ) in build_source
    assert '--opt "build-arg:VERSION={{inputs.parameters.version}}"' in build_source
    assert "#{{inputs.parameters.source-sha}}" in build_source
    assert "echo \"$VERSION\" > /tmp/version" in templates["resolve-version"]["script"]["source"]
    assert "COPY VERSION ." in (ROOT / "Dockerfile").read_text()


def test_docker_hub_registry_secret_is_mounted_only_for_buildkit():
    workflow = _workflow()
    templates = _templates(workflow)

    assert workflow["metadata"]["namespace"] == "argo-workflows"
    volumes = {volume["name"]: volume for volume in workflow["spec"]["volumes"]}
    assert volumes["docker-config"]["secret"] == {
        "secretName": "docker-hub-registry",
        "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
    }

    docker = templates["docker-build"]["container"]
    assert docker["volumeMounts"] == [
        {
            "name": "docker-config",
            "mountPath": "/home/user/.docker",
            "readOnly": True,
        }
    ]
    env = _env_by_name(docker)
    assert env["GIT_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "forgejo-webhook-token",
        "key": "token",
    }
    assert env["DOCKER_CONFIG"]["value"] == "/home/user/.docker"
    assert "DOCKERHUB_TOKEN" not in env
    assert "DOCKER_PASSWORD" not in env

    build_source = docker["args"][0]
    assert "type=image,name=ronaldraygun/git-activity-exporter:" in build_source
    assert "--secret id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN" in build_source
    assert "ronaldraygun/cache" not in build_source
    assert "docker-hub-registry" not in build_source

    # A registry credential must not leak into a source-test, version, or
    # GitOps-promotion pod through a future mount or env entry.
    for template_name in ("test", "resolve-version", "promote", "verify-rollout"):
        template = templates[template_name]
        assert all(
            mount.get("name") != "docker-config"
            for mount in template.get("volumeMounts", [])
        )
        assert "DOCKERHUB_TOKEN" not in _env_by_name(template)
        assert "DOCKER_PASSWORD" not in _env_by_name(template)


def test_missing_or_invalid_registry_credentials_cannot_reach_gitops_promotion():
    workflow = _workflow()
    templates = _templates(workflow)
    steps = [group[0]["name"] for group in templates["build"]["steps"]]

    # The required Secret/key is not optional, so a missing credential fails
    # pod admission before Kaniko runs. An invalid/read-only PAT makes Kaniko
    # fail non-zero. In either case Argo must not continue to promotion.
    docker = templates["docker-build"]
    registry_volume = next(
        volume for volume in workflow["spec"]["volumes"] if volume["name"] == "docker-config"
    )
    assert registry_volume["secret"].get("optional") is not True
    assert "continueOn" not in docker
    assert "continueOn" not in templates["build"]
    assert steps.index("docker-build") < steps.index("promote")

    docker_source = "\n".join(docker.get("container", {}).get("args", []))
    for forbidden in ("set -x", "printenv", "env |", "cat /home/user/.docker/config.json"):
        assert forbidden not in docker_source

    promote_source = templates["promote"].get("script", {}).get("source", "")
    assert "docker-hub-registry" not in promote_source
    assert "/home/user/.docker" not in promote_source


def test_release_fixture_redacts_fake_secrets_from_clone_push_buildkit_surfaces(
    tmp_path: Path,
):
    """Exercise command argv and output surfaces with deliberately fake secrets."""
    workflow = _fixture_workflow()
    templates = _templates(workflow)
    fixture_text = (FIXTURES / "git-activity-exporter-workflow.yml").read_text()
    _assert_no_fake_secret(fixture_text, "workflow fixture")

    # Secret values must be injected only by Kubernetes. They cannot be Argo
    # parameters or output paths, which are persisted and surfaced by Argo.
    parameters = list(workflow["spec"].get("arguments", {}).get("parameters", []))
    for template in templates.values():
        parameters.extend(template.get("inputs", {}).get("parameters", []))
        for output in template.get("outputs", {}).get("parameters", []):
            assert output["name"] not in SECRET_ENV_NAMES
            value_from = output.get("valueFrom", {})
            output_path = value_from.get("path", "")
            assert not SECRET_ENV_NAMES.intersection(output_path.split("/"))
            _assert_no_fake_secret(str(output), f"{template['name']} output")
    assert all(parameter["name"] not in SECRET_ENV_NAMES for parameter in parameters)

    # A credential helper is allowed to mention the Forgejo token because Git
    # expands it inside the helper. Every other script must keep injected
    # values out of logging/tracing and command interpolation.
    for template in templates.values():
        _assert_secret_outputs_are_not_printed(template)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" >> \"$TRACE_FILE\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    trace_file = tmp_path / "git-argv.txt"
    command_env = os.environ.copy()
    command_env.update(FAKE_RELEASE_SECRETS)
    command_env["PATH"] = f"{fake_bin}{os.pathsep}{command_env['PATH']}"
    command_env["TRACE_FILE"] = str(trace_file)

    # Run the actual clone/fetch/push command lines through a fake Git binary.
    # If a token is interpolated into a URL or argument, the fake binary records
    # that exact value and this assertion fails without contacting a provider.
    git_command_count = 0
    for template_name in ("test", "resolve-version", "self-hosting-smoke", "promote"):
        source = templates[template_name].get("script", {}).get("source", "")
        source += templates[template_name].get("container", {}).get("args", [""])[0]
        for operation, command in _release_git_commands(source):
            result = subprocess.run(
                ["sh", "-c", f"set -eu\n{command}"],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
                env=command_env,
            )
            assert result.returncode == 0, result.stderr
            git_command_count += 1
            _assert_no_fake_secret(result.stdout, f"{template_name} {operation} stdout")
            _assert_no_fake_secret(result.stderr, f"{template_name} {operation} stderr")
            if operation == "clone":
                arguments = shlex.split(command)
                urls = [argument for argument in arguments if "://" in argument]
                assert urls, command
                for url in urls:
                    assert "@" not in url
                    assert not any(name in url for name in SECRET_ENV_NAMES)
    assert git_command_count
    _assert_no_fake_secret(trace_file.read_text(), "captured Git argv")

    # BuildKit receives the Forgejo token as a secret session attachment and
    # Docker Hub credentials through a mounted file. Neither value belongs in
    # buildctl argv, output, or image metadata.
    docker = templates["docker-build"]["container"]
    build_source = _render_workflow_parameters(docker["args"][0])
    assert not any(secret in build_source for secret in FAKE_RELEASE_SECRETS.values())
    assert not re.search(r"\$(?:\{)?(?:FORGEJO_TOKEN|GIT_AUTH_TOKEN)\}?", build_source)
    fake_buildctl = fake_bin / "buildctl"
    fake_buildctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    fake_buildctl.chmod(0o755)
    buildkit_result = subprocess.run(
        ["sh", "-ec", build_source],
        check=False,
        capture_output=True,
        text=True,
        env=command_env,
    )
    assert buildkit_result.returncode == 0
    _assert_no_fake_secret(buildkit_result.stdout, "captured buildctl argv")
    _assert_no_fake_secret(buildkit_result.stderr, "captured buildctl stderr")

    image_metadata = "\n".join(
        [str(ROOT / "Dockerfile"), (ROOT / "Dockerfile").read_text(), build_source]
    )
    _assert_no_fake_secret(image_metadata, "image metadata")


def test_partial_release_retries_keep_the_resolved_version_and_verification_gate():
    workflow = _fixture_workflow()
    templates = _templates(workflow)
    build_steps = templates["build"]["steps"]

    assert [group[0]["name"] for group in build_steps] == [
        "test",
        "resolve-version",
        "docker-build",
        "self-hosting-smoke",
        "smoke",
        "promote",
        "verify-rollout",
    ]
    assert build_steps[2][0]["arguments"]["parameters"][0]["value"] == (
        "{{steps.resolve-version.outputs.parameters.version}}"
    )
    assert build_steps[5][0]["arguments"]["parameters"][0]["value"] == (
        "{{steps.resolve-version.outputs.parameters.version}}"
    )
    assert build_steps[5][0]["arguments"]["parameters"][1]["value"] == (
        "{{steps.smoke.outputs.parameters.verified-image}}"
    )

    for name in ("docker-build", "promote"):
        retry = templates[name]["retryStrategy"]
        assert retry["retryPolicy"] == "Always"
        assert retry["limit"] == "2"

    smoke = templates["smoke"]
    assert smoke["outputs"]["parameters"][0]["name"] == "verified-image"
    assert 'printf \'%s\\n\' "$IMAGE" > /tmp/verified-image' in smoke[
        "container"
    ]["args"][0]

    promote = templates["promote"]
    promote_source = promote["script"]["source"]
    assert 'test "$VERIFIED_IMAGE" = "$IMAGE"' in promote_source
    assert "git diff --cached --quiet" in promote_source
    assert 'git rev-parse HEAD > /tmp/gitops-revision' in promote_source


def test_resolver_reuses_an_auto_bump_on_retry_without_a_duplicate_commit(tmp_path):
    workflow = _fixture_workflow()
    application_origin = _application_origin(tmp_path, explicit_version_change=False)
    trigger_sha = _git(
        tmp_path, "--git-dir", str(application_origin), "rev-parse", "main"
    )

    first = _run_resolve_version(
        workflow,
        tmp_path,
        application_origin,
        tmp_path / "first-version",
        commit_sha=trigger_sha,
    )
    second = _run_resolve_version(
        workflow,
        tmp_path,
        application_origin,
        tmp_path / "second-version",
        commit_sha=trigger_sha,
    )

    assert first == second == "1.2.4"
    bump_sha = _git(
        tmp_path, "--git-dir", str(application_origin), "rev-parse", "main"
    )
    assert Path(f"{tmp_path / 'first-version'}.sha").read_text().strip() == bump_sha
    assert Path(f"{tmp_path / 'second-version'}.sha").read_text().strip() == bump_sha
    commits = _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "log",
        "--format=%s",
        "main",
    ).splitlines()
    assert commits.count("ci: auto-bump version to 1.2.4") == 1


def test_promotion_retry_reuses_existing_gitops_revision(tmp_path):
    workflow = _fixture_workflow()
    gitops_origin = _gitops_repository(tmp_path)

    first_revision, first_image = _run_promotion(
        workflow, tmp_path, gitops_origin, "1.2.4", attempt="promotion-first"
    )
    retry_revision, retry_image = _run_promotion(
        workflow, tmp_path, gitops_origin, "1.2.4", attempt="promotion-retry"
    )

    assert retry_revision == first_revision
    assert retry_image == first_image == (
        "ronaldraygun/git-activity-exporter:1.2.4"
    )
    assert _git(
        tmp_path,
        "--git-dir",
        str(gitops_origin),
        "log",
        "--format=%s",
        "main",
    ).splitlines().count("ci(git-activity-exporter): promote image to 1.2.4") == 1


def test_gitops_promotion_failure_can_retry_the_same_verified_release(tmp_path):
    workflow = _fixture_workflow()
    gitops_origin = _gitops_repository(tmp_path)

    with pytest.raises(AssertionError):
        _run_promotion(
            workflow,
            tmp_path,
            tmp_path / "temporarily-unavailable.git",
            "1.2.4",
            attempt="promotion-failed",
        )

    revision, image = _run_promotion(
        workflow,
        tmp_path,
        gitops_origin,
        "1.2.4",
        attempt="promotion-retry-after-push-failure",
    )

    assert image == "ronaldraygun/git-activity-exporter:1.2.4"
    assert revision == _git(
        tmp_path, "--git-dir", str(gitops_origin), "rev-parse", "main"
    )


def test_promotion_rejects_an_unverified_image(tmp_path):
    workflow = _fixture_workflow()
    gitops_origin = _gitops_repository(tmp_path)

    result = _run_promotion_expect_failure(
        workflow,
        tmp_path,
        gitops_origin,
        "1.2.4",
        "ronaldraygun/git-activity-exporter:1.2.3",
    )

    assert result.returncode != 0
    assert "git-activity-exporter:1.2.4" not in _git(
        tmp_path,
        "--git-dir",
        str(gitops_origin),
        "show",
        "main:k8s/ardenone-cluster/git-activity-exporter/deployment.yml",
    )


def test_partial_release_recovery_is_documented():
    deployment = (ROOT / "docs" / "notes" / "deployment.md").read_text()
    _, _, recovery = deployment.partition("## Recovery after a partial release")
    assert recovery, "deployment.md lost the partial-release recovery section"
    recovery = " ".join(recovery.split())
    for phrase in (
        "reuses that version instead of creating a second auto-bump commit",
        "Retry `promote`",
        "Promotion fails closed",
        "does not create a duplicate commit",
        "A successful promotion is still not a completed release",
    ):
        assert phrase in recovery, f"release recovery contract missing: {phrase}"


@pytest.mark.parametrize(
    "explicit_version_change, expected_version",
    [(False, "1.2.4"), (True, "1.2.9")],
    ids=["automatic-version-bump", "explicit-version-change"],
)
def test_release_paths_keep_one_version_before_gitops_promotion(
    tmp_path: Path, explicit_version_change: bool, expected_version: str
):
    """Exercise both resolver branches and the complete image handoff contract."""
    workflow = _workflow()
    application_origin = _application_origin(
        tmp_path, explicit_version_change=explicit_version_change
    )

    resolved_version = _run_resolve_version(
        workflow, tmp_path, application_origin, tmp_path / "resolved-version"
    )
    source_version = _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "show",
        "main:VERSION",
    ).strip()
    latest_source_commit = _git(
        tmp_path,
        "--git-dir",
        str(application_origin),
        "log",
        "-1",
        "--format=%s",
        "main",
    )

    assert resolved_version == expected_version
    assert source_version == resolved_version
    if explicit_version_change:
        assert latest_source_commit == "trigger release"
    else:
        assert latest_source_commit == (
            f"ci: auto-bump version to {resolved_version}"
        )

    templates = _templates(workflow)
    build_source = templates["docker-build"]["container"]["args"][0].replace(
        "{{inputs.parameters.version}}", resolved_version
    )
    assert (
        f"type=image,name=ronaldraygun/git-activity-exporter:{resolved_version},push=true"
        in build_source
    )
    assert f'--opt "build-arg:VERSION={resolved_version}"' in build_source

    smoke = templates["smoke"]
    assert smoke["container"]["image"] == (
        "ronaldraygun/git-activity-exporter:{{inputs.parameters.version}}"
    )
    assert (
        'test "$(tr -d \'[:space:]\' < /app/VERSION)" '
        '= "{{inputs.parameters.version}}"'
        in smoke["container"]["args"][0]
    )
    # Dockerfile COPY VERSION . places the exact main-branch context value at
    # /app/VERSION; this is checked before promotion is allowed to run below.
    assert "COPY VERSION ." in (ROOT / "Dockerfile").read_text()
    embedded_version = source_version
    assert embedded_version == resolved_version

    gitops_origin = _gitops_repository(tmp_path)
    revision, promoted_image = _run_promotion(
        workflow, tmp_path, gitops_origin, resolved_version
    )
    assert revision == _git(
        tmp_path, "--git-dir", str(gitops_origin), "rev-parse", "main"
    )
    assert promoted_image == (
        f"ronaldraygun/git-activity-exporter:{resolved_version}"
    )


def test_release_workflow_and_kubernetes_manifests_use_explicit_image_pins():
    workflow_images = list(_image_fields(_workflow()))
    deployment_images = [
        image
        for resource in _deployment_resources()
        for image in _image_fields(resource)
    ]

    assert workflow_images
    assert deployment_images
    for image in (*workflow_images, *deployment_images):
        _assert_explicit_image_pin(image)


def test_exporter_manifest_enforces_single_writer_topology():
    resources = _deployment_resources()
    deployments = [
        resource
        for resource in resources
        if resource.get("kind") == "Deployment"
        and resource.get("metadata", {}).get("name") == "git-activity-exporter"
    ]
    assert len(deployments) == 1
    deployment = deployments[0]
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}

    quotas = [
        resource
        for resource in resources
        if resource.get("kind") == "ResourceQuota"
        and resource.get("metadata", {}).get("name")
        == "git-activity-exporter-single-writer"
    ]
    assert len(quotas) == 1
    assert quotas[0]["spec"]["hard"] == {"pods": "1"}
    assert not any(
        resource.get("kind") == "HorizontalPodAutoscaler" for resource in resources
    )


def test_ci_writeback_author_is_excluded_from_build_trigger():
    sensor = _sensor()
    dependency = sensor["spec"]["dependencies"][0]
    filters = dependency["filters"]["data"]

    assert dependency["name"] == "git-activity-exporter-push"
    assert {item["path"]: item for item in filters}["body.ref"]["value"] == [
        "refs/heads/main"
    ]

    author_filter = next(
        item for item in filters if item["path"] == "body.head_commit.author.name"
    )
    assert author_filter["comparator"] == "!="
    assert author_filter["value"] == ["Argo Workflows CI"]

    resolve_source = _templates(_workflow())["resolve-version"]["script"]["source"]
    assert 'git config user.email "github@jedarden.com"' in resolve_source
    assert 'git config user.name "Argo Workflows CI"' in resolve_source


def test_runtime_and_release_ci_use_distinct_forgejo_credentials():
    workflow = _workflow()
    templates = _templates(workflow)
    ci_secret = "forgejo-webhook-token"
    runtime_secret = "git-activity-exporter-forge"

    # The runtime Secret must not cross the workflow boundary through an
    # unexpected env entry, volume, or future template. Checking all nested
    # references makes this guard fail closed when a new CI step is added.
    workflow_secret_names = {
        reference.get("name") for reference in _secret_references(workflow)
    }
    assert runtime_secret not in workflow_secret_names

    # Every CI step that can read or write a Forgejo repository uses the
    # write-capable CI Secret, never the runtime collector Secret.
    for template_name in ("test", "resolve-version", "promote"):
        env = _env_by_name(templates[template_name])
        assert "FORGE_TOKEN" not in env
        ci_token = env["FORGEJO_TOKEN"]
        assert ci_token["valueFrom"]["secretKeyRef"] == {
            "name": ci_secret,
            "key": "token",
        }
        assert ci_secret != runtime_secret

    buildkit_env = _env_by_name(templates["docker-build"]["container"])
    assert "FORGE_TOKEN" not in buildkit_env
    assert buildkit_env["GIT_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": ci_secret,
        "key": "token",
    }

    # The runtime Deployment gets only the read-scope token and must not
    # inherit the CI write-back Secret or environment variable.
    runtime_resources = []
    runtime_containers = []
    for resource in _deployment_resources():
        containers = (
            resource.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        exporter_containers = [
            container for container in containers if container.get("name") == "exporter"
        ]
        if exporter_containers:
            runtime_resources.append(resource)
            runtime_containers.extend(exporter_containers)
    assert runtime_containers
    for container in runtime_containers:
        env = _env_by_name(container)
        runtime_token = env["FORGE_TOKEN"]
        assert runtime_token["valueFrom"]["secretKeyRef"] == {
            "name": runtime_secret,
            "key": "FORGE_TOKEN",
        }
        assert "FORGEJO_TOKEN" not in env
        assert all(
            entry.get("valueFrom", {}).get("secretKeyRef", {}).get("name")
            != ci_secret
            for entry in env.values()
        )

    # Check the whole exporter pod, not just its current env list. A future
    # secret volume or envFrom entry must not smuggle in the CI write token.
    runtime_secret_names = {
        reference.get("name")
        for resource in runtime_resources
        for reference in _secret_references(resource)
    }
    assert runtime_secret in runtime_secret_names
    assert ci_secret not in runtime_secret_names


def test_runtime_source_has_no_repository_write_operation():
    """Keep the exporter implementation read-only even as release CI evolves."""
    for path in (ROOT / "src").glob("*.py"):
        source = path.read_text()
        assert not re.search(
            r"\[\s*['\"]git['\"][^\]]*['\"](?:push|commit|add|remote)['\"]",
            source,
        ), f"runtime source gained a repository write command: {path}"
