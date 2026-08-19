"""Roll commits and bead events into one (repo, hour) fact table.

THE WHOLE SCOPE HIERARCHY DERIVES FROM THIS ONE GRAIN. Ecosystem, family and
repo tiers are all sums over the same rows, computed in the browser -- there
are no per-tier files to drift out of agreement with each other. It is
affordable because the grid is overwhelmingly sparse: measured 2026-08-17,
3,432 non-empty cells out of 202,439 possible over 30 days (1.7%), because a
median hour sees only 4 repos active at once.
"""
from datetime import datetime, timezone

from .families import family_of

_EMPTY = {
    "commits": 0, "bulk_commits": 0,
    "lines_added": 0, "lines_deleted": 0,
    "lines_added_raw": 0, "lines_deleted_raw": 0,
    "files_changed": 0,
    "beads_closed": 0, "beads_closed_bulk": 0,
    "beads_claimed": 0, "beads_released": 0, "beads_reopened": 0,
}


def _hour_iso(hour_epoch: int) -> str:
    return datetime.fromtimestamp(hour_epoch * 3600, timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def build_hourly(commits, events, family_map):
    cells = {}
    workers = {}

    def cell(repo, hour):
        key = (repo, hour)
        if key not in cells:
            cells[key] = dict(_EMPTY)
        return cells[key]

    for c in commits:
        h = c["ts"] // 3600
        cur = cell(c["repo"], h)
        cur["commits"] += 1
        cur["lines_added_raw"] += c["lines_added_raw"]
        cur["lines_deleted_raw"] += c["lines_deleted_raw"]
        if c["is_bulk"]:
            cur["bulk_commits"] += 1
        else:
            # A bulk commit contributes its raw lines (so raw stays a true
            # total) but no filtered lines -- that is the entire point of the
            # flag.
            cur["lines_added"] += c["lines_added"]
            cur["lines_deleted"] += c["lines_deleted"]
            cur["files_changed"] += c["files_changed"]

    for e in events:
        h = e["ts"] // 3600
        cur = cell(e["repo"], h)
        kind = e["kind"]
        if kind == "closed":
            if e.get("is_bulk_import"):
                cur["beads_closed_bulk"] += 1
            else:
                cur["beads_closed"] += 1
        elif kind == "claimed":
            cur["beads_claimed"] += 1
            if e.get("actor"):
                workers.setdefault((e["repo"], h), set()).add(e["actor"])
        elif kind == "released":
            cur["beads_released"] += 1
        elif kind == "reopened":
            cur["beads_reopened"] += 1

    rows = []
    for (repo, hour), agg in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        rows.append({
            "hour_utc": _hour_iso(hour),
            "hour_epoch": hour,
            "repo": repo,
            "family": family_of(family_map, repo),
            "workers_active": len(workers.get((repo, hour), ())),
            **agg,
        })
    return rows


def commit_rows(commits, family_map):
    out = []
    for c in commits:
        out.append({
            "sha": c["sha"],
            "repo": c["repo"],
            "family": family_of(family_map, c["repo"]),
            "ts_utc": datetime.fromtimestamp(c["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_utc": _hour_iso(c["ts"] // 3600),
            "author_email": c["author_email"],
            "subject": c["subject"],
            "bead_id": c["bead_id"],
            "lines_added": c["lines_added"],
            "lines_deleted": c["lines_deleted"],
            "files_changed": c["files_changed"],
            "lines_added_raw": c["lines_added_raw"],
            "lines_deleted_raw": c["lines_deleted_raw"],
            "is_bulk": c["is_bulk"],
        })
    return out


def bead_event_rows(events, family_map):
    out = []
    for e in events:
        out.append({
            "ts_utc": datetime.fromtimestamp(e["ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_utc": _hour_iso(e["ts"] // 3600),
            "repo": e["repo"],
            "family": family_of(family_map, e["repo"]),
            "issue_id": e["issue_id"],
            "kind": e["kind"],
            "actor": e["actor"],
            "is_bulk_import": bool(e.get("is_bulk_import")),
        })
    return out
