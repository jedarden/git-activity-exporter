import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"


def test_dockerfile_preserves_runtime_container_contract():
    text = DOCKERFILE.read_text()

    assert re.search(r"^COPY VERSION \.?$", text, re.MULTILINE)
    assert re.search(r"^COPY families\.yaml \.?$", text, re.MULTILINE)
    assert "useradd -r -u 1000 appuser" in text
    assert re.search(r"^USER appuser$", text, re.MULTILINE)
    assert re.search(r"^EXPOSE 8080$", text, re.MULTILINE)
    assert "HEALTHCHECK" in text
    assert "http://localhost:8080/health" in text
    assert 'CMD ["python", "-m", "src.main"]' in text
