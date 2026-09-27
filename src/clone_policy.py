"""Safety policy for Forgejo repository clone URLs."""
from urllib.parse import SplitResult, urlsplit


ALLOWED_SCHEMES = frozenset({"http", "https"})


class CloneURLPolicyError(ValueError):
    """A clone URL is not an endpoint authorized by the Forgejo origin."""


def _split(value: str, label: str, *, require_path: bool) -> SplitResult:
    if not isinstance(value, str) or not value.strip():
        raise CloneURLPolicyError(f"{label} must be a non-empty URL")
    if any(ord(character) < 0x20 for character in value):
        raise CloneURLPolicyError(f"{label} contains control characters")
    try:
        parts = urlsplit(value)
        # Accessing these properties performs validation for malformed ports
        # and bracketed IPv6 hosts.
        hostname = parts.hostname
        port = parts.port
    except ValueError as error:
        raise CloneURLPolicyError(f"{label} is not a valid URL") from error
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise CloneURLPolicyError(
            f"{label} uses a disallowed URL scheme"
        )
    if hostname is None or not hostname:
        raise CloneURLPolicyError(f"{label} must include a host")
    if parts.username is not None or parts.password is not None:
        raise CloneURLPolicyError(f"{label} must not include credentials")
    if parts.query or parts.fragment:
        raise CloneURLPolicyError(f"{label} must not include a query or fragment")
    if require_path and not parts.path:
        raise CloneURLPolicyError(f"{label} must include a path")
    return parts


def _effective_port(parts: SplitResult) -> int:
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme.lower() == "https" else 80


def validate_clone_url(clone_url: str, forge_base_url: str) -> None:
    """Require a clone URL to stay on the configured Forgejo origin.

    Forgejo's API supplies the URL, but the exporter supplies the token to Git.
    The URL is therefore only trusted when it uses the configured origin's
    HTTP(S) scheme, hostname, and effective port. The function deliberately
    avoids including either URL in its errors because a URL may contain
    credentials supplied by a malformed upstream response.
    """
    source = validate_forge_base_url(forge_base_url)
    clone = _split(clone_url, "clone_url", require_path=True)

    if clone.scheme.lower() != source.scheme.lower():
        raise CloneURLPolicyError(
            "clone_url scheme does not match the Forgejo base URL"
        )
    if clone.hostname.casefold() != source.hostname.casefold():
        raise CloneURLPolicyError(
            "clone_url host does not match the Forgejo base URL"
        )
    if _effective_port(clone) != _effective_port(source):
        raise CloneURLPolicyError(
            "clone_url port does not match the Forgejo base URL"
        )


def validate_forge_base_url(forge_base_url: str) -> SplitResult:
    """Validate and parse the configured Forgejo HTTP(S) origin."""
    return _split(forge_base_url, "Forgejo base URL", require_path=False)
