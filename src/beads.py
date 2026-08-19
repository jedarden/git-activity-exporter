"""Bead events, read from each mirror's checked-in forensic log.

`.beads/checkpoint/forensic.jsonl` is git-tracked, so it arrives with the
mirror -- no second data source, no bead CLI, no access to any host's live
SQLite store. `git show HEAD:<path>` reads it straight out of a bare mirror
(verified 2026-08-17 against NEEDLE's mirror: 4,936 records).

TWO PROPERTIES OF THIS DATA THAT THE CHARTS MUST RESPECT.

1. It has an epoch, not a history. Every forensic log in the fleet begins
   2026-08-14, the bead-rs migration; the prior bf ids were discarded by the
   replay. Git backfills 90 days on day one, beads cannot. The panel states
   its own bead epoch rather than drawing an empty left half.

2. The migration flushed its backlog as closures at write time: three hours
   on 2026-08-14 hold 14,566 of 16,650 closed events (87%). Left in, one cell
   dwarfs every real hour -- the ecosystem Fano factor reads 4674 with the
   spike and 47.9 without. Cells above bead_bulk_close_threshold are flagged,
   not deleted, so the UI can toggle them and the total stays reconcilable.
"""
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

FORENSIC_PATH = ".beads/checkpoint/forensic.jsonl"
COUNTED_KINDS = ("closed", "claimed", "released", "reopened")


def read_events(mirror_path: str, repo_name: str, window_days: int, timeout: int):
    """Bead events in the window, or [] if this repo has no forensic log.

    Only 64 of 97 repos that committed in the last 30 days carry one
    (measured 2026-08-17), so absence is the normal case for a third of the
    fleet and must not be an error."""
    proc = subprocess.run(
        ["git", "-C", mirror_path, "show", f"HEAD:{FORENSIC_PATH}"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    events, malformed = [], 0
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if record.get("record_type") != "event":
            continue
        e = record.get("event") or {}
        kind = e.get("kind")
        if kind not in COUNTED_KINDS:
            continue
        raw_time = e.get("time")
        if not raw_time:
            malformed += 1
            continue
        try:
            ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            malformed += 1
            continue
        if ts < cutoff:
            continue
        events.append({
            "repo": repo_name,
            "ts": int(ts.timestamp()),
            "issue_id": e.get("issue_id"),
            "kind": kind,
            # Only `claimed` carries a real worker identity; closed/released/
            # updated/reopened are all actor "system" (measured 2026-08-17:
            # claimed 2683/2683 attributable, every other kind 0%). Worker
            # attribution therefore exists on claims alone -- inferring who
            # closed a bead means joining claim->close on issue_id, which is
            # wrong whenever a bead is released and re-claimed.
            "actor": e.get("actor"),
        })

    if malformed:
        log.warning("%s: skipped %d malformed forensic record(s)", repo_name, malformed)
    return events


def mark_bulk_hours(events, threshold: int, bulk_hour_share: float = 0.5):
    """Flag every closure in a (repo, hour) cell whose closure count exceeds
    threshold. Density is the signal, not the date: a hard-coded migration
    date would miss the next bulk import, and would also wrongly bury the
    genuine work done on the migration day itself."""
    closed_per_cell = {}
    for e in events:
        if e["kind"] == "closed":
            cell = (e["repo"], e["ts"] // 3600)
            closed_per_cell[cell] = closed_per_cell.get(cell, 0) + 1

    bulk_cells = {c for c, n in closed_per_cell.items() if n > threshold}

    # SECOND PASS -- fleet-wide contagion.
    # The per-repo threshold alone leaks. The bead-rs migration flushed every
    # workspace at once, and plenty of individual repos landed just under the
    # bar in those hours (measured: telegram-claude-bridge 141, zai-proxy 123,
    # botburrow 111, nixos-asterisk 102) while 6,549 closures in the very same
    # hour were already flagged. Per-repo they look like a big hour; summed at
    # ecosystem scope they put a 545-closure spike on a chart whose next
    # busiest hour is 81. A repo closing beads inside an hour the fleet was
    # demonstrably bulk-importing is part of that same event, so an hour where
    # flagged closures already dominate marks the whole hour.
    per_hour_total, per_hour_bulk = {}, {}
    for (repo, hour), n in closed_per_cell.items():
        per_hour_total[hour] = per_hour_total.get(hour, 0) + n
        if (repo, hour) in bulk_cells:
            per_hour_bulk[hour] = per_hour_bulk.get(hour, 0) + n

    contaminated = {
        h for h, total in per_hour_total.items()
        if total and per_hour_bulk.get(h, 0) / total >= bulk_hour_share
    }
    bulk_cells |= {c for c in closed_per_cell if c[1] in contaminated}

    for e in events:
        e["is_bulk_import"] = (
            e["kind"] == "closed" and (e["repo"], e["ts"] // 3600) in bulk_cells
        )
    return events, bulk_cells
