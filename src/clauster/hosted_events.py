"""Discriminated ``TypedDict`` shapes for the hosted-channel control plane.

Two internal event tiers flow between the claustrum client and the browser,
both keyed on a ``"type"`` discriminant:

- **Tier 1 — daemon-stream events** (:class:`DaemonStreamEvent`): produced by
  :class:`~clauster.claustrum_client.ProcessStream` as it demuxes the raw socket
  frames, consumed by :meth:`~clauster.hosted.HostedSession._pump`.
- **Tier 2 — hosted events** (:class:`HostedEvent`): produced by
  :meth:`~clauster.hosted.HostedSession._emit`, fanned out to browser
  WebSocket watchers. ``_emit`` stamps an ``event_seq`` onto each payload, so
  these shapes describe the payload *before* that field is added.

Plus the outbound :class:`StdinFrame` shapes clauster writes to the agent.

These annotate the *producer* call sites so a malformed event dict fails
type-checking at construction. The untrusted inbound boundaries stay ``Any`` on
purpose (see #872 A4/A5): the parsed agent stdout frame, the coerced exit code,
and the redacted ``frame``/``request`` payloads embedded in Tier-2 events are
attacker-influenced and are validated with ``isinstance`` at runtime, not typed
away here.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# -- Tier 1: daemon-stream events (ProcessStream -> HostedSession._pump) -------


class StreamLineEvent(TypedDict):
    """One reassembled output line on a daemon stream channel."""

    type: Literal["line"]
    stream: str
    seq: int
    line: str


class StreamExitEvent(TypedDict):
    """The daemon process's terminal exit, carrying its frame seq."""

    type: Literal["exit"]
    seq: int
    exit_code: int | None


#: Tier-1 events broadcast by ``ProcessStream``. The overflow marker
#: (``{"type": overflow_type, "dropped": ...}``) is deliberately excluded: its
#: discriminant is a runtime-chosen string (``"overflow"`` on the client channel,
#: ``"gap"`` on the hosted channel), so it stays an untyped marker offered
#: directly by ``_Subscriber.offer`` rather than a Literal-tagged member here.
DaemonStreamEvent = StreamLineEvent | StreamExitEvent


# -- Tier 2: hosted events (HostedSession._emit -> browser) --------------------


class ControlResolvedEvent(TypedDict):
    """A parked control request was answered (or interrupted)."""

    type: Literal["control_resolved"]
    request_id: str
    behavior: str | None


class LostEvent(TypedDict):
    """The session lost its daemon backing and is no longer answerable."""

    type: Literal["lost"]
    reason: str


class GapDroppedEvent(TypedDict):
    """A count of events dropped by a full subscriber queue (``_emit`` path)."""

    type: Literal["gap"]
    dropped: int


class GapRangeEvent(TypedDict):
    """A first-view replay gap: the ring had already evicted past the cursor."""

    type: Literal["gap"]
    from_seq: int
    to_seq: int


class StderrEvent(TypedDict):
    """A sanitized stderr line from the agent."""

    type: Literal["stderr"]
    text: str


class TextEvent(TypedDict):
    """A sanitized non-JSON stdout line, forwarded as opaque text."""

    type: Literal["text"]
    text: str


class FrameEvent(TypedDict):
    """A redacted agent stdout frame. ``frame`` is untrusted content — kept ``Any``."""

    type: Literal["frame"]
    frame: Any


class ControlAckEvent(TypedDict):
    """An auto-acknowledged handshake control request (e.g. ``initialize``)."""

    type: Literal["control_ack"]
    request_id: str
    subtype: str


class ControlRequestEvent(TypedDict):
    """A parked control request awaiting an operator decision (fail-closed)."""

    type: Literal["control_request"]
    request_id: str
    subtype: str
    request: Any


class ExitEvent(TypedDict):
    """The hosted session's terminal exit, surfaced to watchers."""

    type: Literal["exit"]
    exit_code: int | None


#: Payloads accepted by ``HostedSession._emit`` (before it stamps ``event_seq``).
#: ``GapRangeEvent`` is excluded — it is offered directly by ``subscribe`` on a
#: first-view replay, never through ``_emit``.
HostedEvent = (
    ControlResolvedEvent
    | LostEvent
    | GapDroppedEvent
    | StderrEvent
    | TextEvent
    | FrameEvent
    | ControlAckEvent
    | ControlRequestEvent
    | ExitEvent
)


# -- Outbound stdin frames (clauster -> agent) ---------------------------------


class UserMessage(TypedDict):
    """The ``message`` envelope of a user-turn stdin frame."""

    role: Literal["user"]
    content: str


class UserInputFrame(TypedDict):
    """One user turn written to the agent's stream-json stdin."""

    type: Literal["user"]
    message: UserMessage


class ControlResponseBody(TypedDict):
    """The ``response`` envelope of a success control-response frame."""

    subtype: Literal["success"]
    request_id: str
    response: dict[str, Any]


class ControlResponseFrame(TypedDict):
    """A success ``control_response`` written to the agent's stdin."""

    type: Literal["control_response"]
    response: ControlResponseBody


#: Frames clauster serializes to the agent's stdin.
StdinFrame = UserInputFrame | ControlResponseFrame
