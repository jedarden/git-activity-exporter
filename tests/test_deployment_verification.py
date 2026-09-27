import ast
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-gitops-deployment.sh"
DEPLOYMENT_DOC = ROOT / "docs" / "notes" / "deployment.md"
SMOKE = ROOT / "scripts" / "smoke-self-hosting.sh"



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

def test_deployment_runbook_documents_browser_data_path_and_smoke():
    text = DEPLOYMENT_DOC.read_text()
    _, _, browser = text.partition("## Browser-facing dashboard data path")
    assert browser, "deployment.md lost the browser-facing data path section"
    browser = browser.split("\n## ", 1)[0]
    for phrase in (
        "https://dashboard.ardenone.com/git-activity/data/current.json",
        "Garage's website port",
        "https://s3.ardenone.com",
        "dashboard-write-key",
        "Cache-Control: no-cache",
        "max-age=31536000, immutable",
        "No CORS configuration is required",
        "single atomic PUT",
        "scripts/smoke-self-hosting.sh",
    ):
        assert phrase in browser, f"browser data-path contract lost: {phrase}"

    smoke = SMOKE.read_text()
    for phrase in (
        "BROWSER_HOST=dashboard.example.test",
        "/git-activity/data",
        "current.json",
        "Host: $BROWSER_HOST",
        "urljoin(pointer_url, relative_key)",
        "Cache-Control",
        "no-cache, max-age=0, must-revalidate",
        "public, max-age=31536000, immutable",
    ):
        assert phrase in smoke, f"browser smoke lost required check: {phrase}"
