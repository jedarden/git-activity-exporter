import pytest

from src import config


ENV_NAMES = (
    "FORGE_BASE_URL",
    "FORGE_OWNER",
    "FORGE_TOKEN",
    "REPO_DENYLIST",
    "CLONE_ROOT",
    "WINDOW_DAYS",
    "SHALLOW_SINCE_DAYS",
    "TRIM_MAX_LINES",
    "TRIM_MAX_FILES",
    "EXCLUDED_PATH_PATTERNS",
    "BEAD_BULK_CLOSE_THRESHOLD",
    "BEAD_BULK_HOUR_SHARE",
    "FAMILIES_FILE",
    "MAX_FAILURE_RATE",
    "DEST_S3_ENDPOINT",
    "DEST_S3_BUCKET",
    "DEST_S3_ACCESS_KEY_ID",
    "DEST_S3_SECRET_ACCESS_KEY",
    "DEST_S3_REGION",
    "DEST_S3_ADDRESSING_STYLE",
    "DEST_S3_PREFIX",
    "VERSION_FILE",
    "POLL_INTERVAL_SECONDS",
    "GIT_TIMEOUT_SECONDS",
    "HTTP_TIMEOUT_SECONDS",
    "HEALTH_PORT",
    "LOG_LEVEL",
)

NUMERIC_ENV_NAMES = (
    "WINDOW_DAYS",
    "SHALLOW_SINCE_DAYS",
    "TRIM_MAX_LINES",
    "TRIM_MAX_FILES",
    "BEAD_BULK_CLOSE_THRESHOLD",
    "BEAD_BULK_HOUR_SHARE",
    "MAX_FAILURE_RATE",
    "POLL_INTERVAL_SECONDS",
    "GIT_TIMEOUT_SECONDS",
    "HTTP_TIMEOUT_SECONDS",
    "HEALTH_PORT",
)


@pytest.fixture
def isolated_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "FORGE_TOKEN": "forge-token",
        "DEST_S3_ENDPOINT": "https://s3.test",
        "DEST_S3_BUCKET": "activity-bucket",
        "DEST_S3_ACCESS_KEY_ID": "access-key",
        "DEST_S3_SECRET_ACCESS_KEY": "secret-key",
    }.items():
        monkeypatch.setenv(name, value)


def test_load_uses_documented_defaults(isolated_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("test-version\n")

    assert config.load() == config.Config(
        forge_base_url="https://git.ardenone.com",
        forge_owner="jedarden",
        forge_token="forge-token",
        repo_denylist=[],
        clone_root="/data/mirrors",
        window_days=90,
        shallow_since_days=100,
        trim_max_lines=5000,
        trim_max_files=200,
        excluded_path_patterns=list(config.DEFAULT_EXCLUDED_PATHS),
        bead_bulk_close_threshold=150,
        bead_bulk_hour_share=0.5,
        families_file="families.yaml",
        max_failure_rate=0.2,
        dest=config.S3Endpoint(
            endpoint_url="https://s3.test",
            access_key_id="access-key",
            secret_access_key="secret-key",
            bucket="activity-bucket",
            addressing_style="virtual",
            region="us-east-1",
        ),
        dest_prefix="git-activity/data",
        version="test-version",
        poll_interval_seconds=3600,
        git_timeout_seconds=600,
        http_timeout_seconds=30,
        health_port=8080,
        log_level="INFO",
    )


@pytest.mark.parametrize("name", NUMERIC_ENV_NAMES)
def test_invalid_numeric_values_are_rejected(isolated_env, monkeypatch, name):
    monkeypatch.setenv(name, "not-a-number")

    with pytest.raises(ValueError):
        config.load()


def test_shallow_window_default_follows_custom_window(isolated_env, monkeypatch):
    monkeypatch.setenv("WINDOW_DAYS", "14")

    cfg = config.load()

    assert cfg.window_days == 14
    assert cfg.shallow_since_days == 24


@pytest.mark.parametrize("shallow_since_days", [90, 100])
def test_shallow_window_may_cover_reporting_window(
    isolated_env, monkeypatch, shallow_since_days
):
    monkeypatch.setenv("SHALLOW_SINCE_DAYS", str(shallow_since_days))

    assert config.load().shallow_since_days == shallow_since_days


def test_shallow_window_may_not_undercut_reporting_window(
    isolated_env, monkeypatch
):
    monkeypatch.setenv("SHALLOW_SINCE_DAYS", "89")

    with pytest.raises(config.ConfigError, match="must be >= WINDOW_DAYS"):
        config.load()


def test_denylist_is_trimmed_comma_separated_names(isolated_env, monkeypatch):
    monkeypatch.setenv("REPO_DENYLIST", " repo-one, repo-two ,, repo-three, ")

    assert config.load().repo_denylist == ["repo-one", "repo-two", "repo-three"]
