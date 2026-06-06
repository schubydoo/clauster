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
) -> str:
    """Render the Clauster metrics exposition as a Prometheus text-format string.

    Exposes ``clauster_build_info`` (info gauge), ``clauster_bridges`` (a gauge per
    :class:`InstanceStatus`, 0 when none have that status), and
    ``clauster_projects_total`` (discovered-project count). All values derive from
    the supplied live state; nothing is invented.
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

    lines.append("# HELP clauster_projects_total Number of discovered projects.")
    lines.append("# TYPE clauster_projects_total gauge")
    lines.append(f"clauster_projects_total {project_count}")

    # A trailing newline is conventional for the exposition format.
    return "\n".join(lines) + "\n"
