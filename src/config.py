import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"missing required env var: {name}")
    return value


def _optional(name, default):
    value = os.environ.get(name, "").strip()
    return value if value else default


def _csv(name):
    raw = os.environ.get(name, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else []


# Regexes, not globs -- they must match at any depth. Overridable wholesale
# via EXCLUDED_PATH_PATTERNS (comma-separated) when a repo needs different
# rules, but the default set is what the measurement above justifies.
DEFAULT_EXCLUDED_PATHS = (
    r"(^|/)\.beads/",
    r"(^|/)(vendor|node_modules|third_party|\.venv)/",
    r"(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|go\.sum|uv\.lock|composer\.lock)$",
    r"\.(min\.js|min\.css|map)$",
)


@dataclass(frozen=True)
class S3Endpoint:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    addressing_style: str
    region: str


@dataclass(frozen=True)
class Config:
    forge_base_url: str
    forge_owner: str
    forge_token: str
    repo_denylist: list
    clone_root: str
    window_days: int
    # Clones are bounded by --shallow-since so the mirror set stays small.
    # Full mirrors of every repo measured 14.04 GiB on 2026-08-17, dominated
    # by a handful of artifact-heavy repos (agent-transcript-archive 3.4 GiB,
    # domain-check 2.4 GiB). The dashboard only ever reads a rolling window,
    # so paying for the full history buys nothing.
    shallow_since_days: int
    # A commit above EITHER bound is counted but excluded from the trimmed
    # line totals. Measured 2026-08-17 over an 82h window: raw lines
    # 11,410,214 vs 473,008 trimmed -- 96% of raw volume came from vendored
    # trees and generated files. Summing raw lines yields one visible bar and
    # 14,000 invisible ones.
    trim_max_lines: int
    trim_max_files: int
    # Paths whose line churn is machine-generated and must not count as work.
    # This filter dominates the size trim, not the other way round: measured
    # 2026-08-17 over 30 days and 105 repos, of 61,083,980 lines changed,
    # `.beads/` bookkeeping alone was 41,626,557 (68.1%) and vendored trees a
    # further 3,180,287 (5.2%). Only 26.5% of raw line volume is plausibly
    # hand-written. Counting raw lines measures checkpoint churn, not work.
    excluded_path_patterns: list
    # Closures per (repo, hour) above which the cell is flagged as a bulk
    # import rather than real work. Measured 2026-08-17: the bead-rs
    # migration produced cells up to 1,966 closures, while the busiest
    # genuine hour any repo has ever recorded is 69. 150 sits in the gap.
    bead_bulk_close_threshold: int
    # Fraction of an hour's fleet-wide closures that must already be flagged
    # before the whole hour is treated as bulk. Catches the repos that sat
    # just under the per-repo bar during the same mass import.
    bead_bulk_hour_share: float
    families_file: str
    dest: S3Endpoint
    dest_prefix: str
    version: str
    poll_interval_seconds: int
    git_timeout_seconds: int
    http_timeout_seconds: int
    health_port: int
    log_level: str


def _read_version(version_file):
    try:
        with open(version_file) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def load() -> Config:
    dest = S3Endpoint(
        endpoint_url=_require("DEST_S3_ENDPOINT"),
        access_key_id=_require("DEST_S3_ACCESS_KEY_ID"),
        secret_access_key=_require("DEST_S3_SECRET_ACCESS_KEY"),
        bucket=_require("DEST_S3_BUCKET"),
        # Garage has no per-bucket virtual-host DNS, same as every sibling
        # exporter writing to this bucket.
        addressing_style=_optional("DEST_S3_ADDRESSING_STYLE", "virtual"),
        region=_optional("DEST_S3_REGION", "us-east-1"),
    )

    window_days = int(_optional("WINDOW_DAYS", "90"))
    shallow_since_days = int(_optional("SHALLOW_SINCE_DAYS", str(window_days + 10)))
    if shallow_since_days < window_days:
        raise ConfigError(
            f"SHALLOW_SINCE_DAYS ({shallow_since_days}) must be >= WINDOW_DAYS "
            f"({window_days}); a shallower clone than the reporting window "
            f"silently truncates the oldest hours of every chart"
        )

    return Config(
        forge_base_url=_optional("FORGE_BASE_URL", "https://git.ardenone.com").rstrip("/"),
        forge_owner=_optional("FORGE_OWNER", "jedarden"),
        forge_token=_require("FORGE_TOKEN"),
        repo_denylist=_csv("REPO_DENYLIST"),
        clone_root=_optional("CLONE_ROOT", "/data/mirrors"),
        window_days=window_days,
        shallow_since_days=shallow_since_days,
        trim_max_lines=int(_optional("TRIM_MAX_LINES", "5000")),
        trim_max_files=int(_optional("TRIM_MAX_FILES", "200")),
        excluded_path_patterns=_csv("EXCLUDED_PATH_PATTERNS") or list(DEFAULT_EXCLUDED_PATHS),
        bead_bulk_close_threshold=int(_optional("BEAD_BULK_CLOSE_THRESHOLD", "150")),
        bead_bulk_hour_share=float(_optional("BEAD_BULK_HOUR_SHARE", "0.5")),
        families_file=_optional("FAMILIES_FILE", "families.yaml"),
        dest=dest,
        dest_prefix=_optional("DEST_S3_PREFIX", "git-activity/data").rstrip("/"),
        version=_read_version(_optional("VERSION_FILE", "VERSION")),
        poll_interval_seconds=int(_optional("POLL_INTERVAL_SECONDS", "3600")),
        git_timeout_seconds=int(_optional("GIT_TIMEOUT_SECONDS", "600")),
        http_timeout_seconds=int(_optional("HTTP_TIMEOUT_SECONDS", "30")),
        health_port=int(_optional("HEALTH_PORT", "8080")),
        log_level=_optional("LOG_LEVEL", "INFO"),
    )
