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
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from . import gitscan
from .window import ReportingWindow, as_utc

FORENSIC_PATH = ".beads/checkpoint/forensic.jsonl"
CURRENT_PATH = ".beads/checkpoint/current.json"
NON_EVENT_RECORD_FIELDS = {
    "issue": "issue",
    "attempt_outcome": "attempt_outcome",
    "provenance_receipt": "provenance_receipt",
    "redaction_finding": "redaction_finding",
    "redaction_acknowledgment": "redaction_acknowledgment",
    "redaction_receipt": "redaction_receipt",
    "redaction_epoch": "redaction_epoch",
    "redaction_tombstone": "redaction_tombstone",
}

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


class ForensicParseError(ValueError):
    pass


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _read_head_blob(mirror_path: str, path: str, timeout: int) -> Optional[str]:
    tree = gitscan._run(
        ["git", "-C", mirror_path, "ls-tree", "-z", "HEAD", "--", path], timeout
    )
    if not tree:
        return None

    metadata, separator, _ = tree.rstrip("\0").partition("\t")
    fields = metadata.split()
    if (
        not separator
        or len(fields) != 3
        or fields[1] != "blob"
        or fields[0] not in {"100644", "100755"}
    ):
        raise ForensicParseError(f"checkpoint path {path} is not a regular Git blob")

    return gitscan._run(
        ["git", "-C", mirror_path, "show", f"HEAD:{path}"], timeout
    )


def _validate_forensic_manifest(text: str, manifest_text: str, repo_name: str) -> None:
    try:
        manifest = json.loads(manifest_text, parse_constant=_reject_json_constant)
    except ValueError as error:
        raise ForensicParseError(
            f"{repo_name}: malformed forensic checkpoint manifest"
        ) from error
    if not isinstance(manifest, dict):
        raise ForensicParseError(
            f"{repo_name}: forensic checkpoint manifest is not an object"
        )

    active_root = manifest.get("active_root")
    total_records = manifest.get("total_record_count")
    if not isinstance(active_root, dict) or not isinstance(active_root.get("sha256"), str):
        raise ForensicParseError(
            f"{repo_name}: forensic checkpoint manifest has invalid active_root"
        )
    if isinstance(total_records, bool) or not isinstance(total_records, int):
        raise ForensicParseError(
            f"{repo_name}: forensic checkpoint manifest has invalid total_record_count"
        )

    actual_records = sum(1 for line in text.split("\n") if line.strip())
    if actual_records != total_records:
        raise ForensicParseError(
            f"{repo_name}: forensic record count does not match checkpoint manifest"
        )
    actual_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual_sha256 != active_root["sha256"]:
        raise ForensicParseError(
            f"{repo_name}: forensic checksum does not match checkpoint manifest"
        )


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
    text = _read_head_blob(mirror_path, FORENSIC_PATH, timeout)
    if text is None:
        return []

    manifest_text = _read_head_blob(mirror_path, CURRENT_PATH, timeout)
    if manifest_text is not None:
        _validate_forensic_manifest(text, manifest_text, repo_name)

    return parse_events(
        text, repo_name, reporting_window.start, window_end=reporting_window.end
    )


def _event_timestamp(raw_time: str) -> datetime:
    if raw_time.isascii() and raw_time.isdecimal():
        return datetime.fromtimestamp(int(raw_time), timezone.utc)
    return datetime.fromisoformat(raw_time.replace("Z", "+00:00"))


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

    events = []
    seen = set()
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line, parse_constant=_reject_json_constant)
        except ValueError as error:
            raise ForensicParseError(
                f"{repo_name}: malformed forensic JSON at line {line_number}"
            ) from error
        if not isinstance(record, dict):
            raise ForensicParseError(
                f"{repo_name}: forensic record at line {line_number} is not an object"
            )
        record_type = record.get("record_type")
        if not isinstance(record_type, str) or not record_type:
            raise ForensicParseError(
                f"{repo_name}: forensic record at line {line_number} has invalid record_type"
            )
        if record_type != "event":
            field = NON_EVENT_RECORD_FIELDS.get(record_type)
            if field is None or not isinstance(record.get(field), dict):
                raise ForensicParseError(
                    f"{repo_name}: forensic record at line {line_number} has invalid or unsupported record_type"
                )
            continue

        event = record.get("event")
        if not isinstance(event, dict):
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} is not an object"
            )

        workspace = event.get("origin_store_uuid")
        sequence = event.get("origin_event_sequence")
        issue_id = event.get("issue_id")
        kind = event.get("kind")
        raw_time = event.get("time")
        actor = event.get("actor")
        detail = event.get("detail")

        if not isinstance(workspace, str) or not workspace:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid origin_store_uuid"
            )
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid origin_event_sequence"
            )
        if issue_id is not None and not isinstance(issue_id, str):
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid issue_id"
            )
        if not isinstance(kind, str) or not kind:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid kind"
            )
        if not isinstance(raw_time, str) or not raw_time:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid time"
            )
        if actor is not None and not isinstance(actor, str):
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid actor"
            )
        stated_status = detail.get("resulting_base_status") if isinstance(detail, dict) else None
        if stated_status is not None and not isinstance(stated_status, str):
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid resulting_base_status"
            )

        identity = (workspace, sequence)
        if identity in seen:
            raise ForensicParseError(
                f"{repo_name}: duplicate forensic event identity at line {line_number}"
            )
        seen.add(identity)

        try:
            ts = _event_timestamp(raw_time)
        except (OSError, OverflowError, ValueError) as error:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid time"
            ) from error
        resulting_status = _resulting_status(kind, detail)

        try:
            in_window = reporting_window.contains(ts)
        except (TypeError, ValueError) as error:
            raise ForensicParseError(
                f"{repo_name}: forensic event at line {line_number} has invalid time"
            ) from error
        if not in_window:
            continue

        events.append({
            "repo": repo_name,
            "ts": int(ts.timestamp()),
            "workspace_uuid": workspace,
            "issue_id": issue_id,
            "kind": kind,
            "actor": actor,
            "resulting_status": resulting_status,
        })

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
