#!/usr/bin/env python3
"""Validate versioned release image references.

The release workflow treats VERSION as the source of truth for the published
image. The self-hosting examples are committed documentation and fixtures, so
their literal image tags must move with that source version as well. Every
image in the release manifests also needs an explicit non-latest tag or digest.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
EXPORTER_IMAGE = re.compile(
    r"(?P<name>(?:[A-Za-z0-9.-]+/)*git-activity-exporter(?:[A-Za-z0-9._-]*))"
    r":(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)"
)
YAML_IMAGE_FIELD = re.compile(r"^\s*image:\s*(?P<value>.+?)\s*$", re.MULTILINE)
DOCKER_FROM = re.compile(
    r"^\s*FROM(?:\s+--platform=\S+)?\s+(?P<value>\S+)", re.MULTILINE
)
IMAGE_DESTINATION = re.compile(r"--destination=(?P<value>[^\s'\"]+)")

VERSIONED_FILES = (
    Path("examples/self-hosting/compose.yaml"),
    Path("examples/self-hosting/kubernetes.yaml"),
    Path("docs/self-hosting.md"),
    Path("docs/notes/deployment.md"),
)
LATEST_SCAN_FILES = (
    Path("docs/self-hosting.md"),
    Path("docs/notes/deployment.md"),
    Path("scripts/smoke-self-hosting.sh"),
)
PINNED_IMAGE_FILES = (
    Path("Dockerfile"),
    Path("examples"),
    Path("tests/fixtures/git-activity-exporter-workflow.yml"),
)
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml"}


def _line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _read_version(root: Path) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.fullmatch(version):
        raise ValueError(f"VERSION must contain a semver, got {version!r}")
    return version


def _check_versioned_files(root: Path, version: str) -> list[str]:
    errors: list[str] = []
    for relative_path in VERSIONED_FILES:
        path = root / relative_path
        if not path.is_file():
            errors.append(f"{relative_path}: file is missing")
            continue

        text = path.read_text(encoding="utf-8")
        matches = list(EXPORTER_IMAGE.finditer(text))
        if not matches:
            errors.append(f"{relative_path}: no exporter image reference found")
            continue
        for match in matches:
            tag = match.group("tag")
            if tag != version:
                line = _line_number(text, match.start())
                errors.append(
                    f"{relative_path}:{line}: image tag {tag!r} does not match "
                    f"VERSION {version!r}"
                )
    return errors


def _text_files(root: Path, relative_path: Path):
    path = root / relative_path
    if path.is_file():
        yield relative_path, path
        return
    if not path.is_dir():
        return
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix in TEXT_SUFFIXES:
            yield child.relative_to(root), child


def _check_latest_references(root: Path) -> list[str]:
    errors: list[str] = []
    scanned: set[Path] = set()

    for relative_path in LATEST_SCAN_FILES:
        for discovered_path, path in _text_files(root, relative_path):
            if discovered_path in scanned:
                continue
            scanned.add(discovered_path)
            text = path.read_text(encoding="utf-8")

            # YAML image fields include both direct references and Compose's
            # ${VAR:-image:tag} form. A :latest tag is never acceptable.
            for match in YAML_IMAGE_FIELD.finditer(text):
                if ":latest" in match.group("value"):
                    line = _line_number(text, match.start())
                    errors.append(
                        f"{discovered_path}:{line}: image references :latest"
                    )

            # Documentation and shell snippets do not have YAML image fields,
            # but explicit exporter references there must obey the same rule.
            for match in EXPORTER_IMAGE.finditer(text):
                if match.group("tag") == "latest":
                    line = _line_number(text, match.start())
                    errors.append(
                        f"{discovered_path}:{line}: image references :latest"
                    )
    return errors


def _image_value(value: str) -> str:
    """Resolve the concrete image in a YAML/Compose value when possible."""
    value = value.split("#", 1)[0].strip().strip("'\"")
    default = re.search(r":-([^}]+)}", value)
    if default:
        return default.group(1)
    return value


def _check_image_reference(
    relative_path: Path, text: str, position: int, value: str
) -> str | None:
    image = _image_value(value)
    line = _line_number(text, position)
    if not image or image.startswith("$"):
        return f"{relative_path}:{line}: image reference {value!r} has no concrete tag"

    name, separator, digest = image.partition("@")
    last_component = name.rsplit("/", 1)[-1]
    if ":" not in last_component and not separator:
        return f"{relative_path}:{line}: image reference {image!r} has no tag"

    tag = last_component.rsplit(":", 1)[1] if ":" in last_component else ""
    if tag.lower() == "latest":
        return f"{relative_path}:{line}: image references :latest (mutable tag {image!r})"
    if separator and not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9+.-]*:[0-9a-fA-F]+", digest
    ):
        return f"{relative_path}:{line}: image reference {image!r} has invalid digest"
    return None


def _check_pinned_image_references(root: Path) -> list[str]:
    errors: list[str] = []
    discovered: set[Path] = set()

    for relative_path in PINNED_IMAGE_FILES:
        paths = list(_text_files(root, relative_path))
        if not paths:
            errors.append(f"{relative_path}: file or directory is missing")
            continue

        for discovered_path, path in paths:
            if discovered_path in discovered:
                continue
            discovered.add(discovered_path)
            text = path.read_text(encoding="utf-8")

            if discovered_path.name == "Dockerfile":
                references = [
                    (match.start("value"), match.group("value"))
                    for match in DOCKER_FROM.finditer(text)
                ]
            else:
                references = [
                    (match.start("value"), match.group("value"))
                    for match in YAML_IMAGE_FIELD.finditer(text)
                ]
                references.extend(
                    (match.start("value"), match.group("value"))
                    for match in IMAGE_DESTINATION.finditer(text)
                )

            for position, value in references:
                error = _check_image_reference(
                    discovered_path, text, position, value
                )
                if error:
                    errors.append(error)
    return errors


def _rewrite_versioned_files(root: Path, version: str) -> list[Path]:
    changed: list[Path] = []
    for relative_path in VERSIONED_FILES:
        path = root / relative_path
        text = path.read_text(encoding="utf-8")
        rewritten = EXPORTER_IMAGE.sub(
            lambda match: f"{match.group('name')}:{version}", text
        )
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")
            changed.append(relative_path)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check self-hosting image tags against VERSION."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: the current repository)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite versioned exporter image references to VERSION before checking",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    try:
        version = _read_version(root)
        image_errors = _check_pinned_image_references(root)
        latest_errors = _check_latest_references(root)
        if image_errors or latest_errors:
            for error in (*image_errors, *latest_errors):
                print(f"ERROR: {error}", file=sys.stderr)
            return 1

        if args.write:
            changed = _rewrite_versioned_files(root, version)
            for path in changed:
                print(f"updated {path}")

        errors = _check_versioned_files(root, version)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"release image references match VERSION {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
