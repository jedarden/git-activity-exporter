import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import aggregate, beads, config, families, forge, gitscan, parquet_io, s3io

log = logging.getLogger(__name__)

_published = threading.Event()


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
            self.send_response(200)
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


def _collect(cfg, family_map):
    """One enumeration + scan pass. Returns (repos, commits, events, stats).

    A per-repo failure never aborts the cycle: the repo is skipped, counted
    into stats, and the publish guard in _run_cycle decides whether the
    partial result may publish. The full semantics -- timeout vs corruption,
    stale mirrors, orphan pruning, persistent failure -- are specified in
    docs/notes/data-sources.md "Failure semantics"; this function is where
    they are applied.
    """
    repos = forge.list_repos(
        cfg.forge_base_url, cfg.forge_token, cfg.forge_owner,
        cfg.http_timeout_seconds, cfg.repo_denylist,
    )

    # Orphan hygiene runs against the fresh listing: deleted, renamed,
    # denylisted or emptied repos stop costing PVC this cycle. Names pruned
    # are surfaced in meta.json so a deletion is auditable.
    mirrors_pruned = gitscan.prune_orphans(cfg.clone_root, [r["name"] for r in repos])

    all_commits, all_events = [], []
    scanned, failed, stale, with_beads = 0, [], [], 0
    repo_errors = {}

    for repo in repos:
        name = repo["name"]
        try:
            path, refreshed = gitscan.ensure_mirror(
                repo, cfg.clone_root, cfg.forge_token, cfg.shallow_since_days, cfg.git_timeout_seconds
            )
            commits = gitscan.scan_commits(
                path, name, cfg.window_days, cfg.excluded_path_patterns, cfg.git_timeout_seconds
            )
            events = beads.read_events(path, name, cfg.window_days, cfg.git_timeout_seconds)
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
        "mirrors_pruned": mirrors_pruned,
        "repos_with_bead_data": with_beads,
    }


def build_meta(cfg, stats, generated_at: str, cycle_seconds: float, events, hourly) -> dict:
    """meta.json's exact key set. Extracted from _run_cycle as a pure
    function so tests/test_docs.py can drift-test it against the documented
    example in docs/notes/output-schema.md, the way DEFAULT_EXCLUDED_PATHS
    is drift-tested against configuration.md."""
    bead_epoch = min((e["ts"] for e in events), default=None)
    unassigned = sorted({r["repo"] for r in hourly if r["family"] == families.UNASSIGNED})
    return {
        "version": cfg.version,
        "generated_at": generated_at,
        "window_days": cfg.window_days,
        "repos_total": stats["repos_total"],
        "repos_scanned": stats["repos_scanned"],
        "repos_failed": stats["repos_failed"],
        "repo_errors": stats["repo_errors"],
        "repos_stale": stats["repos_stale"],
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
            if bead_epoch else None
        ),
        "bulk_bead_cells": stats["bulk_bead_cells"],
        "unassigned_repos": unassigned,
        "trim_max_lines": cfg.trim_max_lines,
        "trim_max_files": cfg.trim_max_files,
        "excluded_path_patterns": cfg.excluded_path_patterns,
    }


def _run_cycle(cfg, s3, family_map):
    started = time.monotonic()
    generated_at = _now()
    repos, commits, events, stats = _collect(cfg, family_map)

    gitscan.mark_bulk(commits, cfg.trim_max_lines, cfg.trim_max_files)
    events, bulk_cells = beads.mark_bulk_hours(
        events, cfg.bead_bulk_close_threshold, cfg.bead_bulk_hour_share
    )
    stats["bulk_bead_cells"] = len(bulk_cells)

    hourly = aggregate.build_hourly(commits, events, family_map)
    log.info(
        "cycle: %d/%d repos scanned (%d failed, %d stale), %d commits, %d bead events, %d hourly cells",
        stats["repos_scanned"], stats["repos_total"], len(stats["repos_failed"]),
        len(stats["repos_stale"]), len(commits), len(events), len(hourly),
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
            raise RuntimeError(
                f"{len(failed)}/{total} repo(s) failed this cycle "
                f"({failure_rate:.0%} > {cfg.max_failure_rate:.0%} limit); refusing to "
                f"publish over the previous cycle's data. First failures: {failed[:3]}"
            )
        if failed:
            log.warning(
                "publishing with %d/%d repo(s) missing (%.0f%%, under the %.0f%% limit)",
                len(failed), total, failure_rate * 100, cfg.max_failure_rate * 100,
            )

    for key, rows, schema in (
        ("hourly.parquet", hourly, parquet_io.HOURLY_SCHEMA),
        ("commits.parquet", aggregate.commit_rows(commits, family_map), parquet_io.COMMITS_SCHEMA),
        ("bead_events.parquet", aggregate.bead_event_rows(events, family_map), parquet_io.BEAD_EVENTS_SCHEMA),
    ):
        s3io.upload_bytes(
            s3, cfg.dest.bucket, f"{cfg.dest_prefix}/{key}",
            parquet_io.table_to_parquet_bytes(rows, schema), "application/octet-stream",
        )

    meta = build_meta(cfg, stats, generated_at, time.monotonic() - started, events, hourly)
    s3io.upload_bytes(
        s3, cfg.dest.bucket, f"{cfg.dest_prefix}/meta.json",
        json.dumps(meta, indent=2).encode(), "application/json",
    )


def main():
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    family_map = families.load(cfg.families_file)
    s3 = s3io.client(cfg.dest)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    _serve_health(cfg.health_port)

    while not stop.is_set():
        try:
            _run_cycle(cfg, s3, family_map)
            _published.set()
        except Exception:
            log.exception("cycle failed, will retry next interval")
        stop.wait(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
