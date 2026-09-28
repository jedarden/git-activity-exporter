import json
import logging
import signal
import sys
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import aggregate, beads, config, families, forge, gitscan, parquet_io, publish, s3io
from .window import ReportingWindow

log = logging.getLogger(__name__)

_published = threading.Event()
_cycle_state_lock = threading.Lock()
_last_successful_cycle_at = None
_last_cycle_outcome = None
_cycle_attempts = {"published": 0, "withheld": 0, "failed": 0}
_publication_failures_total = 0
_consecutive_publication_failures = 0
_poll_interval_seconds = 3600
_process_start_time_seconds = time.time()
_current_reporting_window = ContextVar("reporting_window", default=None)


class CycleWithheld(RuntimeError):
    """The cycle completed collection but was rejected by the publish guard."""


def _reset_cycle_state():
    global _last_successful_cycle_at, _last_cycle_outcome
    global _publication_failures_total, _consecutive_publication_failures
    global _poll_interval_seconds
    with _cycle_state_lock:
        _last_successful_cycle_at = None
        _last_cycle_outcome = None
        _cycle_attempts.update(published=0, withheld=0, failed=0)
        _publication_failures_total = 0
        _consecutive_publication_failures = 0
        _poll_interval_seconds = 3600
    publish.reset_prune_health()


def _record_cycle_outcome(outcome: str, successful_cycle_at: Optional[str] = None):
    """Publish the operator-facing state used by the health endpoint.

    A failed or withheld cycle must not move the freshness timestamp backward:
    it describes the newest cycle that actually committed a complete dataset.
    """
    global _last_successful_cycle_at, _last_cycle_outcome
    global _consecutive_publication_failures
    with _cycle_state_lock:
        _last_cycle_outcome = outcome
        _cycle_attempts[outcome] += 1
        if outcome == "published":
            _last_successful_cycle_at = successful_cycle_at
            # A complete publication is the recovery boundary for a run of
            # publication errors. Withheld cycles do not reset this counter:
            # they are not successful publications either.
            _consecutive_publication_failures = 0


def _record_publication_failure():
    """Record a failed publication separately from collection failures."""
    global _publication_failures_total, _consecutive_publication_failures
    with _cycle_state_lock:
        _publication_failures_total += 1
        _consecutive_publication_failures += 1


def _set_poll_interval_seconds(seconds: int):
    global _poll_interval_seconds
    with _cycle_state_lock:
        _poll_interval_seconds = seconds


def _health_snapshot():
    with _cycle_state_lock:
        snapshot = {
            "last_successful_cycle_at": _last_successful_cycle_at,
            "last_cycle_outcome": _last_cycle_outcome,
        }
    snapshot["prune"] = publish.prune_health()
    return snapshot


def _timestamp_seconds(value: Optional[str]) -> float:
    if value is None:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _metrics_payload() -> bytes:
    """Render the small exporter state surface in Prometheus text format."""
    with _cycle_state_lock:
        successful_at = _last_successful_cycle_at
        outcome = _last_cycle_outcome
        attempts = dict(_cycle_attempts)
        publication_failures_total = _publication_failures_total
        consecutive_publication_failures = _consecutive_publication_failures
        poll_interval_seconds = _poll_interval_seconds

    prune = publish.prune_health()
    lines = [
        "# HELP git_activity_exporter_up Process health; this endpoint is live.",
        "# TYPE git_activity_exporter_up gauge",
        "git_activity_exporter_up 1",
        "# HELP git_activity_exporter_process_start_time_seconds Unix start time.",
        "# TYPE git_activity_exporter_process_start_time_seconds gauge",
        f"git_activity_exporter_process_start_time_seconds {_process_start_time_seconds:.3f}",
        "# HELP git_activity_exporter_poll_interval_seconds Configured post-cycle sleep.",
        "# TYPE git_activity_exporter_poll_interval_seconds gauge",
        f"git_activity_exporter_poll_interval_seconds {poll_interval_seconds}",
        "# HELP git_activity_exporter_last_successful_publication_timestamp_seconds Unix timestamp of the latest committed publication, or 0 before the first one.",
        "# TYPE git_activity_exporter_last_successful_publication_timestamp_seconds gauge",
        f"git_activity_exporter_last_successful_publication_timestamp_seconds {_timestamp_seconds(successful_at):.3f}",
        "# HELP git_activity_exporter_cycle_attempts_total Cycle attempts by terminal outcome.",
        "# TYPE git_activity_exporter_cycle_attempts_total counter",
    ]
    for name in ("published", "withheld", "failed"):
        lines.append(
            f'git_activity_exporter_cycle_attempts_total{{outcome="{name}"}} {attempts[name]}'
        )
    lines.extend([
        "# HELP git_activity_exporter_last_cycle_outcome Current terminal outcome, one for the current outcome.",
        "# TYPE git_activity_exporter_last_cycle_outcome gauge",
    ])
    for name in ("published", "withheld", "failed"):
        lines.append(
            f'git_activity_exporter_last_cycle_outcome{{outcome="{name}"}} {int(outcome == name)}'
        )
    lines.extend([
        "# HELP git_activity_exporter_publication_failures_total Failed publication attempts since process start.",
        "# TYPE git_activity_exporter_publication_failures_total counter",
        f"git_activity_exporter_publication_failures_total {publication_failures_total}",
        "# HELP git_activity_exporter_publication_failures_consecutive Consecutive failed publication attempts.",
        "# TYPE git_activity_exporter_publication_failures_consecutive gauge",
        f"git_activity_exporter_publication_failures_consecutive {consecutive_publication_failures}",
        "# HELP git_activity_exporter_prune_consecutive_failures Consecutive failed post-publication prune attempts.",
        "# TYPE git_activity_exporter_prune_consecutive_failures gauge",
        f"git_activity_exporter_prune_consecutive_failures {prune['consecutive_failures']}",
        "",
    ])
    return "\n".join(lines).encode()


class _HealthHandler(BaseHTTPRequestHandler):
    # Liveness and readiness are deliberately separate here, unlike the
    # sibling exporters that serve one /health returning 503 until the first
    # cycle lands. The first cycle of this exporter clones the entire fleet:
    # NEEDLE alone measured 148.6s cold against 1.21s to fetch once mirrored,
    # so a cold start across ~111 repos runs far past any sane liveness
    # threshold. Gating liveness on it would guarantee a crashloop that never
    # finishes a first clone, and the pod would never become useful.
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps(_health_snapshot(), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/metrics":
            body = _metrics_payload()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path == "/ready":
            self.send_response(200 if _published.is_set() else 503)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


def _serve_health(port: int):
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("health server listening on :%d (/health liveness, /ready readiness)", port)


def _now() -> str:
    # Explicit Z. A naive isoformat() with no offset gets read as local time
    # by browsers and silently shifts every chart.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reporting_window(generated_at: str, window_days: int) -> ReportingWindow:
    anchor = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    return ReportingWindow.from_anchor(anchor, window_days)


def _collect(cfg, family_map, reporting_window: Optional[ReportingWindow] = None):
    """One enumeration + scan pass. Returns (repos, commits, events, stats).

    A per-repo failure never aborts the cycle: the repo is skipped, counted
    into stats, and the publish guard in _run_cycle decides whether the
    partial result may publish. The full semantics -- timeout vs corruption,
    stale mirrors, partial history, orphan pruning, persistent failure -- are
    specified in docs/notes/data-sources.md "Failure semantics"; this function
    is where they are applied.
    """
    if reporting_window is None:
        reporting_window = _current_reporting_window.get()
    if reporting_window is None:
        reporting_window = ReportingWindow.from_anchor(
            datetime.now(timezone.utc), cfg.window_days
        )

    # CLONE_ROOT is destructive state: fail before enumeration, mirror scan,
    # or orphan pruning if the configured directory is not the provisioned
    # writable mirror volume.
    gitscan.validate_clone_root(cfg.clone_root)

    repos = forge.list_repos(
        cfg.forge_base_url, cfg.forge_token, cfg.forge_owner,
        cfg.http_timeout_seconds, cfg.repo_denylist,
    )

    # Orphan hygiene runs against the fresh listing: deleted, renamed,
    # denylisted or emptied repos stop costing PVC this cycle. Names pruned
    # are surfaced in meta.json so a deletion is auditable.
    mirrors_pruned = gitscan.prune_orphans(cfg.clone_root, [r["name"] for r in repos])

    all_commits, all_events = [], []
    scanned, failed, stale, partial_history, with_beads = 0, [], [], [], 0
    repo_errors = {}

    for repo in repos:
        name = repo["name"]
        try:
            path, refreshed = gitscan.ensure_mirror(
                repo, cfg.clone_root, cfg.forge_token, cfg.shallow_since_days,
                cfg.git_timeout_seconds, reporting_window.start,
                cfg.forge_base_url,
            )
            history_complete = gitscan.mirror_history_complete(
                path, reporting_window.start, cfg.shallow_since_days,
                cfg.git_timeout_seconds
            )
            commits = gitscan.scan_commits(
                path, name, cfg.window_days, cfg.excluded_path_patterns,
                cfg.git_timeout_seconds, reporting_window
            )
            events = beads.read_events(
                path, name, cfg.window_days, cfg.git_timeout_seconds,
                reporting_window
            )
        except gitscan.StorageExhausted:
            # A full volume is a cycle-fatal infrastructure error, not a
            # repository-sized gap that the publish guard may accept.
            raise
        except Exception as e:
            # One unreachable or corrupt repo must not cost the whole cycle;
            # the failure is counted into meta.json so a repo silently
            # dropping out of the charts is visible rather than inferred.
            # The reason travels too (truncated; scrubbed of credentials
            # upstream in gitscan._run) so persistence is legible without
            # trawling pod logs.
            error = gitscan._scrub(str(e))
            log.warning("repo %s failed, excluding from this cycle: %s", name, error)
            failed.append(name)
            # Git errors are scrubbed at their source, but sanitize once more
            # at the metadata boundary so a future per-repo error source
            # cannot write a credential into the durable public object.
            repo_errors[name] = error[:200]
            continue

        scanned += 1
        if not refreshed:
            stale.append(name)
        if not history_complete:
            partial_history.append(name)
        if events:
            with_beads += 1
        all_commits.extend(commits)
        all_events.extend(events)

    return repos, all_commits, all_events, {
        "repos_total": len(repos),
        "repos_scanned": scanned,
        "repos_failed": failed,
        "repo_errors": repo_errors,
        # Scanned from a mirror this cycle could not refresh. Deliberately
        # NOT counted as failure: the data is present, merely not newest.
        "repos_stale": stale,
        "repos_partial_history": partial_history,
        "mirrors_pruned": mirrors_pruned,
        "repos_with_bead_data": with_beads,
    }


def build_meta(cfg, stats, generated_at: str, cycle_seconds: float, events, hourly,
               cycle_id: str, attribution_epochs=None) -> dict:
    """meta.json's exact key set. Extracted from _run_cycle as a pure
    function so tests/test_docs.py can drift-test it against the documented
    example in docs/notes/output-schema.md, the way DEFAULT_EXCLUDED_PATHS
    is drift-tested against configuration.md."""
    bead_epoch = min((e["ts"] for e in events), default=None)
    if attribution_epochs is None:
        attribution_epochs = beads.attribution_epochs(events)
    unassigned = sorted({r["repo"] for r in hourly if r["family"] == families.UNASSIGNED})
    attribution_epoch = {
        repo: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for repo, ts in sorted(attribution_epochs.items())
    }
    return {
        "version": cfg.version,
        # The publication this object belongs to, echoed by current.json's
        # cycle_id. A fixed-key consumer can compare the two to notice it is
        # reading across a publication boundary.
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        "window_days": cfg.window_days,
        "repos_total": stats["repos_total"],
        "repos_scanned": stats["repos_scanned"],
        "repos_failed": stats["repos_failed"],
        "repo_errors": stats["repo_errors"],
        "repos_stale": stats["repos_stale"],
        "repos_partial_history": stats.get("repos_partial_history", []),
        "mirrors_pruned": stats["mirrors_pruned"],
        "repos_with_bead_data": stats["repos_with_bead_data"],
        # The bound the failure semantics are defined against, so a consumer
        # diagnosing timeouts can see what the exporter was actually given.
        "git_timeout_seconds": cfg.git_timeout_seconds,
        # Wall-clock health: a cycle that overruns its poll interval is
        # degrading even when every repo in it succeeded.
        "cycle_seconds": round(cycle_seconds, 1),
        # The panel needs this to caption the bead charts honestly: git
        # backfills the full window on first run, beads only exist from the
        # bead-rs migration forward and cannot be reconstructed.
        "bead_epoch_utc": (
            datetime.fromtimestamp(bead_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if bead_epoch is not None else None
        ),
        "attribution_epoch": attribution_epoch,
        "bulk_bead_cells": stats["bulk_bead_cells"],
        "unassigned_repos": unassigned,
        "trim_max_lines": cfg.trim_max_lines,
        "trim_max_files": cfg.trim_max_files,
        "excluded_path_patterns": cfg.excluded_path_patterns,
    }


def _run_cycle(cfg, s3, family_map):
    # A pod may have died after a fixed-key PUT but before the pointer commit.
    # Reconcile from the durable pointer before doing another expensive scan;
    # fixed keys are never trusted as recovery input.
    try:
        publish.recover_publication(
            s3, cfg.dest.bucket, cfg.dest_prefix,
            expected_names=publish.DEFAULT_FIXED_NAMES,
        )
    except Exception as e:
        publish.raise_safe_publication_error("publication recovery failed at cycle start", e)

    started = time.monotonic()
    generated_at = _now()
    reporting_window = _reporting_window(generated_at, cfg.window_days)
    token = _current_reporting_window.set(reporting_window)
    try:
        repos, commits, events, stats = _collect(cfg, family_map)
    finally:
        _current_reporting_window.reset(token)

    gitscan.mark_bulk(commits, cfg.trim_max_lines, cfg.trim_max_files)
    events, bulk_cells = beads.mark_bulk_hours(
        events, cfg.bead_bulk_close_threshold, cfg.bead_bulk_hour_share
    )
    stats["bulk_bead_cells"] = len(bulk_cells)
    attribution_epochs = beads.attribution_epochs(events)

    hourly = aggregate.build_hourly(commits, events, family_map, attribution_epochs)
    log.info(
        "cycle: %d/%d repos scanned (%d failed, %d stale, %d partial history), %d commits, %d bead events, %d hourly cells",
        stats["repos_scanned"], stats["repos_total"], len(stats["repos_failed"]),
        len(stats["repos_stale"]), len(stats.get("repos_partial_history", [])),
        len(commits), len(events), len(hourly),
    )

    # REFUSE TO PUBLISH A BADLY DEGRADED CYCLE.
    # An infrastructure fault -- an unwritable volume, a revoked credential,
    # the forge unreachable -- is not a quiet fleet, and publishing its
    # result overwrites a good dataset with something that reads as "the
    # fleet did much less work", which is indistinguishable from a real lull
    # and far harder to notice than a stale timestamp.
    #
    # An earlier version of this check only fired when EVERY repo failed, and
    # that proved far too weak in practice: when the Forgejo token was
    # rotated, 97 of 112 repos failed authentication but the 15 PUBLIC ones
    # still cloned anonymously, so `scanned` was non-zero, the guard stayed
    # quiet, and a 6,588-cell dataset was replaced by a 1,580-cell one. The
    # threshold is a fraction for that reason -- partial failure is the
    # dangerous case, not total failure.
    if stats["repos_total"]:
        failed = stats["repos_failed"]
        total = stats["repos_total"]
        failure_rate = len(failed) / total
        if failure_rate > cfg.max_failure_rate:
            raise CycleWithheld(
                f"{len(failed)}/{total} repo(s) failed this cycle "
                f"({failure_rate:.0%} > {cfg.max_failure_rate:.0%} limit); refusing to "
                f"publish over the previous cycle's data. First failures: {failed[:3]}"
            )
        if failed:
            log.warning(
                "publishing with %d/%d repo(s) missing (%.0f%%, under the %.0f%% limit)",
                len(failed), total, failure_rate * 100, cfg.max_failure_rate * 100,
            )

    # GENERATE EVERY PAYLOAD BEFORE ANYTHING IS UPLOADED. The pre-protocol
    # loop serialized each table straight into its upload, so a generation
    # failure after the first upload had already replaced one live object
    # with its new-cycle counterpart. Building all four payloads in memory
    # first (a few MiB) means a Parquet or meta failure raises with nothing
    # on S3 touched, and publish.publish_cycle owns the upload-ordering and
    # commit rules (see its docstring for the protocol and why each step is
    # where it is).
    cycle_id = publish.new_cycle_id(generated_at)
    payloads = [
        ("hourly.parquet",
         parquet_io.table_to_parquet_bytes(hourly, parquet_io.HOURLY_SCHEMA),
         "application/octet-stream"),
        ("commits.parquet",
         parquet_io.table_to_parquet_bytes(aggregate.commit_rows(commits, family_map),
                                           parquet_io.COMMITS_SCHEMA),
         "application/octet-stream"),
        ("bead_events.parquet",
         parquet_io.table_to_parquet_bytes(aggregate.bead_event_rows(events, family_map),
                                           parquet_io.BEAD_EVENTS_SCHEMA),
         "application/octet-stream"),
    ]
    meta = build_meta(cfg, stats, generated_at, time.monotonic() - started, events, hourly,
                       cycle_id=cycle_id, attribution_epochs=attribution_epochs)
    payloads.append(("meta.json", json.dumps(meta, indent=2).encode(), "application/json"))

    publish.publish_cycle(s3, cfg.dest.bucket, cfg.dest_prefix, payloads,
                          cycle_id=cycle_id, generated_at=generated_at)
    return generated_at


def main():
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    family_map = families.load(cfg.families_file)
    try:
        s3 = s3io.client(cfg.dest)
    except Exception as error:
        # Client construction happens before the poll loop. Keep startup
        # failures actionable without allowing a provider exception to echo
        # either S3 credential into stderr.
        message = s3io.redact_credentials(str(error), cfg.dest)
        print(f"S3 client creation failed: {message}", file=sys.stderr)
        sys.exit(1)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    _serve_health(cfg.health_port)
    _set_poll_interval_seconds(cfg.poll_interval_seconds)

    s3_permissions_checked = False
    while not stop.is_set():
        try:
            if not s3_permissions_checked:
                s3io.check_permissions(
                    s3, cfg.dest.bucket, cfg.dest_prefix
                )
                s3_permissions_checked = True
                log.info("S3 destination permission preflight passed")
            generated_at = _run_cycle(cfg, s3, family_map)
            _record_cycle_outcome("published", generated_at)
            _published.set()
        except s3io.S3PermissionError as e:
            _record_cycle_outcome("failed")
            # S3PermissionError is deliberately sanitized by s3io. Do not
            # log the provider exception or endpoint, which may contain
            # credentials in a self-hosted configuration.
            log.error("%s", e)
        except gitscan.StorageExhausted as error:
            _record_cycle_outcome("failed")
            # Do not publish the repositories that happened to fit. The
            # previous complete publication remains authoritative while the
            # PVC is expanded or space is reclaimed and the next poll retries.
            log.error("mirror volume exhausted; cycle failed without publication: %s", error)
        except CycleWithheld as e:
            _record_cycle_outcome("withheld")
            log.warning("cycle withheld: %s", e)
        except Exception as error:
            _record_cycle_outcome("failed")
            if isinstance(error, publish.PublicationError):
                _record_publication_failure()
                log.error(
                    "publication failed, will retry next interval: %s",
                    s3io.redact_credentials(str(error), cfg.dest),
                )
            else:
                log.error(
                    "cycle failed, will retry next interval: %s",
                    s3io.redact_credentials(str(error), cfg.dest),
                )
        stop.wait(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
