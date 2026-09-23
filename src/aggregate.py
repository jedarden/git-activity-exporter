"""Roll commits and bead events into one (repo, hour, worker) fact table.

THE WHOLE SCOPE HIERARCHY DERIVES FROM THIS ONE GRAIN. Ecosystem, family and
repo tiers are sums over the null-worker rows, while named worker partitions
are available after each repository's attribution epoch -- all in the browser.
There are no per-tier files to drift out of agreement with each other. It is
affordable because the grid is overwhelmingly sparse: measured 2026-08-17,
3,432 non-empty cells out of 202,439 possible over 30 days (1.7%), because a
median hour sees only 4 repos active at once.
"""
from datetime import datetime, timezone

from .beads import COUNTED_KINDS
from .beads import attribution_epochs as event_attribution_epochs
from .families import family_of

INFERENTIAL_WORKER = "inferential"

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


def _add_event(agg, event):
    kind = event["kind"]
    if kind == "closed":
        if event.get("is_bulk_import"):
            agg["beads_closed_bulk"] += 1
        else:
            agg["beads_closed"] += 1
    elif kind == "claimed":
        agg["beads_claimed"] += 1
    elif kind == "released":
        agg["beads_released"] += 1
    elif kind == "reopened":
        agg["beads_reopened"] += 1


def _worker_label(event, epoch_by_repo):
    epoch = epoch_by_repo.get(event["repo"])
    actor = event.get("actor")
    if (
        epoch is not None
        and event["ts"] >= epoch
        and isinstance(actor, str)
        and actor
        and actor != "system"
    ):
        return actor
    return INFERENTIAL_WORKER


def build_hourly(commits, events, family_map, attribution_epochs=None):
    events = list(events)
    if attribution_epochs is None:
        attribution_epochs = event_attribution_epochs(events)

    cells = {}
    worker_cells = {}
    claimers = {}
    worker_claimers = set()

    def cell(repo, hour):
        key = (repo, hour)
        if key not in cells:
            cells[key] = dict(_EMPTY)
        return cells[key]

    def worker_cell(repo, hour, worker):
        key = (repo, hour, worker)
        if key not in worker_cells:
            worker_cells[key] = dict(_EMPTY)
        return worker_cells[key]

    for c in commits:
        h = c["ts"] // 3600
        cur = cell(c["repo"], h)
        cur["commits"] += 1
        cur["lines_added_raw"] += c["lines_added_raw"]
        cur["lines_deleted_raw"] += c["lines_deleted_raw"]
        if c["is_bulk"]:
            cur["bulk_commits"] += 1
        else:
            cur["lines_added"] += c["lines_added"]
            cur["lines_deleted"] += c["lines_deleted"]
            cur["files_changed"] += c["files_changed"]

    for e in events:
        kind = e["kind"]
        if kind not in COUNTED_KINDS:
            continue
        h = e["ts"] // 3600
        cur = cell(e["repo"], h)
        worker = _worker_label(e, attribution_epochs)
        worker_agg = worker_cell(e["repo"], h, worker)
        _add_event(cur, e)
        _add_event(worker_agg, e)
        if kind == "claimed" and e.get("actor"):
            claimers.setdefault((e["repo"], h), set()).add(e["actor"])
            if worker != INFERENTIAL_WORKER:
                worker_claimers.add((e["repo"], h, worker))

    row_specs = []
    for (repo, hour), agg in cells.items():
        row_specs.append((hour, repo, 0, "", None, agg))
    for (repo, hour, worker), agg in worker_cells.items():
        order = 1 if worker == INFERENTIAL_WORKER else 2
        row_specs.append((hour, repo, order, worker, worker, agg))

    rows = []
    for hour, repo, _, _, worker, agg in sorted(row_specs):
        if worker is None:
            active = len(claimers.get((repo, hour), ()))
        else:
            active = int((repo, hour, worker) in worker_claimers)
        rows.append({
            "hour_utc": _hour_iso(hour),
            "hour_epoch": hour,
            "repo": repo,
            "family": family_of(family_map, repo),
            "worker": worker,
            "workers_active": active,
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
            "workspace_uuid": e.get("workspace_uuid"),
            "issue_id": e["issue_id"],
            "kind": e["kind"],
            "actor": e["actor"],
            "resulting_status": e.get("resulting_status"),
            "is_bulk_import": bool(e.get("is_bulk_import")),
        })
    return out
