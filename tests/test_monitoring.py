from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
MONITORING = ROOT / "examples" / "self-hosting" / "monitoring.yaml"


def _resources():
    return list(yaml.safe_load_all(MONITORING.read_text()))


def test_monitoring_manifest_scrapes_metrics_and_declares_cycle_alerts():
    resources = _resources()
    assert [resource["kind"] for resource in resources] == [
        "Service",
        "ServiceMonitor",
        "PrometheusRule",
    ]

    service, monitor, rule = resources
    assert service["spec"]["selector"] == {"app": "git-activity-exporter"}
    assert service["spec"]["ports"] == [{
        "name": "metrics",
        "port": 8080,
        "targetPort": "health",
        "protocol": "TCP",
    }]
    assert monitor["spec"]["endpoints"] == [{
        "port": "metrics",
        "path": "/metrics",
        "interval": "30s",
        "scrapeTimeout": "10s",
    }]

    alerts = {
        alert["alert"]: alert
        for group in rule["spec"]["groups"]
        for alert in group["rules"]
    }
    assert set(alerts) == {
        "GitActivityExporterPublicationStale",
        "GitActivityExporterWithheldCycles",
        "GitActivityExporterPruneFailures",
        "GitActivityExporterPublicationFailures",
        "GitActivityExporterMetricsMissing",
    }
    assert "last_successful_publication_timestamp_seconds" in alerts[
        "GitActivityExporterPublicationStale"
    ]["expr"]
    assert "cycle_attempts_total" in alerts["GitActivityExporterWithheldCycles"]["expr"]
    assert "prune_consecutive_failures" in alerts["GitActivityExporterPruneFailures"]["expr"]
    assert "publication_failures_consecutive" in alerts[
        "GitActivityExporterPublicationFailures"
    ]["expr"]
    assert "absent(git_activity_exporter_up" in alerts[
        "GitActivityExporterMetricsMissing"
    ]["expr"]
