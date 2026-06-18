"""Unit tests for the Prometheus text-exposition renderer (#352 enrichment)."""

from __future__ import annotations

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.prometheus import render_metrics


def _running(project: str) -> RemoteControlInstance:
    inst = RemoteControlInstance(project=project, label=project)
    inst.status = InstanceStatus.RUNNING
    return inst


def test_base_families_always_present():
    out = render_metrics(version="1.2.3", instances=[], project_count=2)
    assert 'clauster_build_info{version="1.2.3"} 1' in out
    assert "# TYPE clauster_bridges gauge" in out
    assert "clauster_projects 2" in out
    assert out.endswith("\n")


def test_optional_families_omitted_when_not_supplied():
    out = render_metrics(version="1", instances=[], project_count=0)
    for absent in (
        "clauster_bridge_crashes_total",
        "clauster_bridge_cpu_percent",
        "clauster_bridge_rss_bytes",
        "clauster_hosted_sessions",
        "clauster_claustrum_up",
    ):
        assert absent not in out


def test_crash_counter_is_a_counter_per_project():
    out = render_metrics(
        version="1", instances=[], project_count=0, crash_counts={"beta": 1, "alpha": 3}
    )
    assert "# TYPE clauster_bridge_crashes_total counter" in out
    # Sorted by project for stable output.
    a = out.index('clauster_bridge_crashes_total{project="alpha"} 3')
    b = out.index('clauster_bridge_crashes_total{project="beta"} 1')
    assert a < b


def test_bridge_samples_emit_cpu_and_rss_gauges():
    out = render_metrics(
        version="1",
        instances=[_running("alpha")],
        project_count=1,
        bridge_samples=[("alpha", 12.5, 1048576)],
    )
    assert 'clauster_bridge_cpu_percent{project="alpha"} 12.5' in out
    assert 'clauster_bridge_rss_bytes{project="alpha"} 1048576' in out


def test_hosted_and_claustrum_gauges():
    up = render_metrics(
        version="1", instances=[], project_count=0, hosted_sessions=2, claustrum_up=True
    )
    assert "clauster_hosted_sessions 2" in up
    assert "clauster_claustrum_up 1" in up
    down = render_metrics(version="1", instances=[], project_count=0, claustrum_up=False)
    assert "clauster_claustrum_up 0" in down


def test_label_values_are_escaped():
    out = render_metrics(version="1", instances=[], project_count=0, crash_counts={'we"ird\\': 1})
    assert 'project="we\\"ird\\\\"' in out
