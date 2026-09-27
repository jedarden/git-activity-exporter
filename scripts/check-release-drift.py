#!/usr/bin/env python3
"""Validate versioned self-hosting image references.

The release workflow treats VERSION as the source of truth for the published
image. The self-hosting examples are committed documentation and fixtures, so
their literal image tags must move with that source version as well.
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

VERSIONED_FILES = (
    Path("examples/self-hosting/compose.yaml"),
    Path("examples/self-hosting/kubernetes.yaml"),
    Path("docs/self-hosting.md"),
    Path("docs/notes/deployment.md"),
)
LATEST_SCAN_FILES = (
    *VERSIONED_FILES,
    Path("scripts/smoke-self-hosting.sh"),
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
        latest_errors = _check_latest_references(root)
        if latest_errors:
            for error in latest_errors:
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

    print(f"self-hosting release references match VERSION {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
