import json
import logging
import signal
import sys
import threading
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
    repos = forge.list_repos(
        cfg.forge_base_url, cfg.forge_token, cfg.forge_owner,
        cfg.http_timeout_seconds, cfg.repo_denylist,
    )

    all_commits, all_events = [], []
    scanned, failed, with_beads = 0, [], 0

    for repo in repos:
        name = repo["name"]
        try:
            path = gitscan.ensure_mirror(
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
            log.warning("repo %s failed, excluding from this cycle: %s", name, e)
            failed.append(name)
            continue

        scanned += 1
        if events:
            with_beads += 1
        all_commits.extend(commits)
        all_events.extend(events)

    return repos, all_commits, all_events, scanned, failed, with_beads


def _run_cycle(cfg, s3, family_map):
    generated_at = _now()
    repos, commits, events, scanned, failed, with_beads = _collect(cfg, family_map)

    gitscan.mark_bulk(commits, cfg.trim_max_lines, cfg.trim_max_files)
    events, bulk_cells = beads.mark_bulk_hours(
        events, cfg.bead_bulk_close_threshold, cfg.bead_bulk_hour_share
    )

    hourly = aggregate.build_hourly(commits, events, family_map)
    log.info(
        "cycle: %d/%d repos scanned (%d failed), %d commits, %d bead events, %d hourly cells",
        scanned, len(repos), len(failed), len(commits), len(events), len(hourly),
    )

    # REFUSE TO PUBLISH A TOTAL FAILURE.
    # Every repo failing is an infrastructure fault -- an unwritable volume, a
    # dead credential, the forge unreachable -- not a quiet fleet. Uploading
    # the resulting empty tables would overwrite a good dataset with zeroes
    # and render as "no activity", which is indistinguishable from a real
    # lull and far harder to notice than a stale timestamp. Bail instead:
    # readiness stays false, meta.json keeps its previous generated_at, and
    # the panel keeps showing the last good data.
    if repos and not scanned:
        raise RuntimeError(
            f"all {len(repos)} repo(s) failed this cycle; refusing to publish "
            f"empty data over the previous cycle's. First failures: {failed[:3]}"
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

    bead_epoch = min((e["ts"] for e in events), default=None)
    unassigned = sorted({r["repo"] for r in hourly if r["family"] == families.UNASSIGNED})
    meta = {
        "version": cfg.version,
        "generated_at": generated_at,
        "window_days": cfg.window_days,
        "repos_total": len(repos),
        "repos_scanned": scanned,
        "repos_failed": failed,
        "repos_with_bead_data": with_beads,
        # The panel needs this to caption the bead charts honestly: git
        # backfills the full window on first run, beads only exist from the
        # bead-rs migration forward and cannot be reconstructed.
        "bead_epoch_utc": (
            datetime.fromtimestamp(bead_epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if bead_epoch else None
        ),
        "bulk_bead_cells": len(bulk_cells),
        "unassigned_repos": unassigned,
        "trim_max_lines": cfg.trim_max_lines,
        "trim_max_files": cfg.trim_max_files,
        "excluded_path_patterns": cfg.excluded_path_patterns,
    }
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
