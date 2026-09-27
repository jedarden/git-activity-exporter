import ast
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-gitops-deployment.sh"
DEPLOYMENT_DOC = ROOT / "docs" / "notes" / "deployment.md"


def test_post_reconcile_verifier_is_executable_and_has_help():
    assert SCRIPT.stat().st_mode & 0o111, "verification helper must be executable"
    result = subprocess.run(
        [str(SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0
    assert "--app APP" in result.stdout
    assert "--revision GITOPS_COMMIT" in result.stdout
    assert "--image REGISTRY/IMAGE:SEMVER" in result.stdout


def test_post_reconcile_verifier_is_read_only_and_checks_every_release_boundary():
    text = SCRIPT.read_text()
    snippets = re.findall(r"<<'PY'\n(.*?)\nPY", text, re.DOTALL)
    assert len(snippets) == 4
    for snippet in snippets:
        ast.parse(snippet)

    for phrase in (
        "argocd app wait",
        "--sync --health",
        "argocd app get",
        'sync.get("revision") != expected_revision',
        'kubectl --namespace "$NAMESPACE" get deployment',
        'kubectl --namespace "$NAMESPACE" get pods',
        '"path": "/health"',
        '"path": "/ready"',
        "kubectl --namespace \"$NAMESPACE\" port-forward --address 127.0.0.1",
        'last_cycle_outcome") != "published"',
        "last_successful_cycle_at",
    ):
        assert phrase in text, f"verifier lost required check: {phrase}"

    for forbidden in (
        "kubectl apply",
        "kubectl create",
        "kubectl delete",
        "kubectl patch",
        "kubectl edit",
        "kubectl rollout undo",
        "kubectl set image",
    ):
        assert forbidden not in text, f"verifier must not mutate the cluster: {forbidden}"


def test_deployment_runbook_documents_post_reconcile_and_rollback():
    text = DEPLOYMENT_DOC.read_text()
    _, _, verification = text.partition("## Post-reconcile verification")
    assert verification, "deployment.md lost the post-reconcile verification section"
    verification = verification.split("\n## ", 1)[0]
    for phrase in (
        "verify-gitops-deployment.sh",
        "Synced",
        "Healthy",
        "semver",
        "last_cycle_outcome",
        "published",
        "git-activity-exporter-ns-ardenone-cluster",
    ):
        assert phrase in verification, f"post-reconcile runbook lost: {phrase}"

    _, _, rollback = text.partition("### Safe rollback")
    assert rollback, "deployment.md lost the safe rollback section"
    rollback = rollback.split("\n## ", 1)[0]
    for phrase in (
        "prior image",
        "declarative-config",
        "git push",
        "verify-gitops-deployment.sh",
        "do not use `kubectl",
    ):
        assert phrase in rollback, f"rollback runbook lost: {phrase}"
