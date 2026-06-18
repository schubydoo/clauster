"""Hand-rolled Prometheus text exposition for the read-only ``/metrics`` endpoint.

Renders a small, fixed set of point-in-time gauges from live runner state in the
Prometheus text format (version 0.0.4): ``# HELP``/``# TYPE`` header lines followed
by ``name{labels} value`` samples. No dependency on ``prometheus_client`` — the
surface is tiny and the format is simple, so the few escaping rules are inlined.
"""

from __future__ import annotations

from .models import InstanceStatus, RemoteControlInstance

# Per the exposition format spec, the content type carries the version and charset.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label_value(value: str) -> str:
    """Escape a label value per the text format (backslash, double-quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics(
    *,
    version: str,
    instances: list[RemoteControlInstance],
    project_count: int,
    bridge_samples: list[tuple[str, float, int]] | None = None,
    crash_counts: dict[str, int] | None = None,
    hosted_sessions: int | None = None,
    claustrum_up: bool | None = None,
) -> str:
    """Render the Clauster metrics exposition as a Prometheus text-format string.

    Exposes ``clauster_build_info`` (info gauge), ``clauster_bridges`` (a gauge per
    :class:`InstanceStatus`), ``clauster_projects`` (discovered-project count), and —
    when supplied — ``clauster_bridge_crashes_total`` (a per-project *counter*, so a
    crash that resumes between scrapes still leaves a trace), per-bridge
    ``clauster_bridge_cpu_percent`` / ``clauster_bridge_rss_bytes`` gauges (from
    ``bridge_samples`` ``(project, cpu, rss)`` triples), ``clauster_hosted_sessions``,
    and ``clauster_claustrum_up``. All values derive from the supplied live state.
    """
    counts: dict[InstanceStatus, int] = {status: 0 for status in InstanceStatus}
    for inst in instances:
        counts[inst.status] += 1

    lines: list[str] = []

    lines.append("# HELP clauster_build_info Build information for the running Clauster.")
    lines.append("# TYPE clauster_build_info gauge")
    lines.append(f'clauster_build_info{{version="{_escape_label_value(version)}"}} 1')

    lines.append("# HELP clauster_bridges Number of managed bridges by lifecycle status.")
    lines.append("# TYPE clauster_bridges gauge")
    for status in InstanceStatus:
        lines.append(
            f'clauster_bridges{{status="{_escape_label_value(status.value)}"}} {counts[status]}'
        )

    # No `_total` suffix: that's reserved for counters. This is a gauge (projects can
    # be removed), so `clauster_projects` keeps promtool / Grafana type-inference happy.
    lines.append("# HELP clauster_projects Number of discovered projects.")
    lines.append("# TYPE clauster_projects gauge")
    lines.append(f"clauster_projects {project_count}")

    if crash_counts:
        lines.append("# HELP clauster_bridge_crashes_total Bridge crashes since process start.")
        lines.append("# TYPE clauster_bridge_crashes_total counter")
        for project, n in sorted(crash_counts.items()):
            label = _escape_label_value(project)
            lines.append(f'clauster_bridge_crashes_total{{project="{label}"}} {n}')

    if bridge_samples:
        lines.append("# HELP clauster_bridge_cpu_percent Per-bridge process-tree CPU percent.")
        lines.append("# TYPE clauster_bridge_cpu_percent gauge")
        for project, cpu, _rss in sorted(bridge_samples):
            label = _escape_label_value(project)
            lines.append(f'clauster_bridge_cpu_percent{{project="{label}"}} {cpu}')
        lines.append("# HELP clauster_bridge_rss_bytes Per-bridge process-tree resident memory.")
        lines.append("# TYPE clauster_bridge_rss_bytes gauge")
        for project, _cpu, rss in sorted(bridge_samples):
            label = _escape_label_value(project)
            lines.append(f'clauster_bridge_rss_bytes{{project="{label}"}} {rss}')

    if hosted_sessions is not None:
        lines.append("# HELP clauster_hosted_sessions Number of live hosted (claustrum) sessions.")
        lines.append("# TYPE clauster_hosted_sessions gauge")
        lines.append(f"clauster_hosted_sessions {hosted_sessions}")

    if claustrum_up is not None:
        lines.append("# HELP clauster_claustrum_up Whether the claustrum daemon is connected.")
        lines.append("# TYPE clauster_claustrum_up gauge")
        lines.append(f"clauster_claustrum_up {1 if claustrum_up else 0}")

    # A trailing newline is conventional for the exposition format.
    return "\n".join(lines) + "\n"
