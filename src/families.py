"""Repo -> family mapping.

The middle scope tier. Purely editorial: no rule derived from repo metadata
groups `commitgraph` with `commitgraph-deprecated`, or the ROTA/HOOP/FORGE
agent tooling into one programme, so the mapping is a checked-in file rather
than something inferred at runtime.

Unmapped repos fall through to `unassigned` rather than raising -- a new repo
must show up in the ecosystem and repo tiers on its first commit, without
anyone having to touch config first. The panel surfaces the unassigned count
so the mapping's drift stays visible instead of silently swallowing repos.
"""
import logging
import os

import yaml

log = logging.getLogger(__name__)

UNASSIGNED = "unassigned"


def load(path: str) -> dict:
    """Returns {repo_name: family}. A missing file is not fatal -- the whole
    fleet just reports as `unassigned`, which is a legible degraded state
    rather than a crashloop."""
    if not os.path.exists(path):
        log.warning("families file %s not found; all repos will map to %r", path, UNASSIGNED)
        return {}

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    families = raw.get("families") or {}
    mapping = {}
    for family, repos in families.items():
        for repo in repos or []:
            if repo in mapping and mapping[repo] != family:
                # Ambiguous membership would make family totals depend on dict
                # ordering, so it is a hard error rather than last-write-wins.
                raise ValueError(
                    f"repo {repo!r} is listed under both {mapping[repo]!r} and {family!r}"
                )
            mapping[repo] = family
    log.info("loaded %d repo->family mappings across %d families", len(mapping), len(families))
    return mapping


def family_of(mapping: dict, repo: str) -> str:
    return mapping.get(repo, UNASSIGNED)
