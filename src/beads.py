"""Bead events, read from each mirror's checked-in forensic log.

`.beads/checkpoint/forensic.jsonl` is git-tracked, so it arrives with the
mirror -- no second data source, no bead CLI, no access to any host's live
SQLite store. `git show HEAD:<path>` reads it straight out of a bare mirror
(verified 2026-08-17 against NEEDLE's mirror: 4,936 records).

THREE PROPERTIES OF THIS DATA THAT THE CHARTS MUST RESPECT.

1. It has an epoch, not a history. Every forensic log in the fleet begins
   2026-08-14, the bead-rs migration; the prior bf ids were discarded by the
   replay. Git backfills 90 days on day one, beads cannot. The panel states
   its own bead epoch rather than drawing an empty left half.

2. The migration flushed its backlog as closures at write time: three hours
   on 2026-08-14 hold 14,566 of 16,650 closed events (87%). Left in, one cell
   dwarfs every real hour -- the ecosystem Fano factor reads 4674 with the
   spike and 47.9 without. Cells above bead_bulk_close_threshold are flagged,
   not deleted, so the UI can toggle them and the total stays reconcilable.

3. Attribution has an epoch, not a history. A repository's attribution epoch
   is its first `closed` event with a non-`system` actor. Events before that
   instant are not back-filled from a claim: a release and re-claim can change
   the actor. Join semantics live in docs/notes/output-schema.md.
"""
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import Optional

from . import gitscan
from .window import ReportingWindow, as_utc

log = logging.getLogger(__name__)

FORENSIC_PATH = ".beads/checkpoint/forensic.jsonl"

# The kinds the (repo, hour) rollup counts. bead_events.parquet is at event
# grain and carries every kind the forensic log records; this filter pins the
# rollup so widening the published event stream cannot move hourly.parquet.
COUNTED_KINDS = ("closed", "claimed", "released", "reopened")

# Resulting statuses taken from the event kind itself. Only `closed` belongs
# here: a close event is what makes a bead closed, and its detail records the
# *prior* status and the reason rather than the result. Every other kind
# either states its own result in detail.resulting_base_status or is not a
# status transition at all.
KIND_RESULTING_STATUS = {"closed": "closed"}


def attribution_epochs(events):
    epochs = {}
    for e in events:
        if e.get("kind") != "closed":
            continue
        actor = e.get("actor")
        if not isinstance(actor, str) or not actor or actor == "system":
            continue
        repo = e["repo"]
        ts = e["ts"]
        if repo not in epochs or ts < epochs[repo]:
            epochs[repo] = ts
    return epochs


def read_events(mirror_path: str, repo_name: str, window_days: int, timeout: int,
                reporting_window: Optional[ReportingWindow] = None):
    """Bead events in the window, or [] if this repo has no forensic log.

    Only 64 of 97 repos that committed in the last 30 days carry one
    (measured 2026-08-17), so absence is the normal case for a third of the
    fleet and must not be an error.

    Every forensic event is kept, not just the kinds the rollup counts: this
    file is one join source of the factory attempt ledger, which reads it at
    event grain (docs/notes/output-schema.md). The rollup filters by
    COUNTED_KINDS downstream."""
    if reporting_window is None:
        reporting_window = ReportingWindow.from_anchor(
            datetime.now(timezone.utc), window_days
        )
    args = ["git", "-C", mirror_path, "show", f"HEAD:{FORENSIC_PATH}"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Keep every git invocation on the same typed timeout path. A missing
        # forensic file is normal and remains a successful empty result below;
        # a timed-out show is different because we cannot tell whether the
        # file was read completely, so the caller excludes the repo.
        raise gitscan.GitTimeout(f"git show timed out after {timeout}s")
    if proc.returncode != 0:
        return []

    return parse_events(
        proc.stdout, repo_name, reporting_window.start, window_end=reporting_window.end
    )


def parse_events(text: str, repo_name: str, cutoff: datetime,
                 window_end: Optional[datetime] = None):
    """Forensic JSONL -> event dicts. Split from read_events so the fixture
    tests exercise the real parse without a git mirror."""
    if isinstance(cutoff, ReportingWindow):
        if window_end is not None:
            raise ValueError("pass either a ReportingWindow or cutoff and window_end")
        reporting_window = cutoff
    else:
        start = as_utc(cutoff)
        end = (
            as_utc(window_end)
            if window_end is not None
            else datetime.max.replace(tzinfo=timezone.utc)
        )
        reporting_window = ReportingWindow(start, end)

    events, malformed = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if record.get("record_type") != "event":
            continue  # issue snapshots are state, not an event to publish
        e = record.get("event") or {}
        kind = e.get("kind")
        raw_time = e.get("time")
        if not kind or not raw_time or not isinstance(raw_time, str):
            malformed += 1
            continue
        try:
            ts = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if not reporting_window.contains(ts):
                continue
        except (TypeError, ValueError):
            malformed += 1
            continue
        events.append({
            "repo": repo_name,
            "ts": int(ts.timestamp()),
            # Identity of the workspace the event happened in. The forensic
            # log's own field is origin_store_uuid; a bead id is only unique
            # inside one workspace, so this is what makes issue_id joinable
            # when two workspaces could ever share a prefix.
            "workspace_uuid": e.get("origin_store_uuid"),
            "issue_id": e.get("issue_id"),
            "kind": kind,
            "actor": e.get("actor"),
            "resulting_status": _resulting_status(kind, e.get("detail")),
        })

    if malformed:
        log.warning("%s: skipped %d malformed forensic record(s)", repo_name, malformed)
    return events


def _resulting_status(kind, detail):
    """The bead's base_status after this event, or None when the event does
    not move it.

    `claimed`, `released` and `reopened` state the result themselves in
    detail.resulting_base_status. A `closed` event's detail records only the
    prior status and the close reason, so the result comes from the kind.
    Everything else -- `created`, `updated`, label and dependency edits --
    carries no transition, and is left null rather than guessed."""
    if isinstance(detail, dict) and detail.get("resulting_base_status"):
        return detail["resulting_base_status"]
    return KIND_RESULTING_STATUS.get(kind)


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
