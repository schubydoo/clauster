"""Which sessions a standard bridge ended, read from its debug log (TASK-557)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clauster import bridge_log
from clauster.models import RemoteControlInstance
from clauster.session_ends import SessionEndTracker

_ENV = "env_01RHE7cHW3DawXjGRp5Ae3va"
_SID = "session_01VxAGVqxjtFiJrYGb63PeiU"
_CSE = "cse_01VxAGVqxjtFiJrYGb63PeiU"
_REGISTERED = (
    f"2026-11-17T01:25:00.000Z [DEBUG] [bridge:api] POST /v1/environments/bridge -> 200 "
    f"environment_id={_ENV}\n"
)
_ARCHIVED = (
    f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] POST /v1/code/sessions/{_CSE}/archive\n"
)


def _log(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "alpha-1-1.log"
    path.write_bytes(text.encode())
    return path


@pytest.mark.parametrize(
    "route",
    [f"/v1/code/sessions/{_CSE}/archive", f"/v1/sessions/{_SID}/archive"],
)
def test_archive_request_ends_the_session_in_either_id_form(tmp_path, route):
    line = f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] POST {route}\n"
    path = _log(tmp_path, _REGISTERED + line)
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is True


def test_a_live_session_has_not_ended(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is False


def test_another_sessions_archive_does_not_end_this_one(tmp_path):
    other = _ARCHIVED.replace(_CSE, "cse_01OTHERSESSIONAAAAAAAAAA")
    path = _log(tmp_path, _REGISTERED + other)
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is False


@pytest.mark.parametrize(
    "line",
    [
        # Session text quoted inside a websocket frame must not count.
        f'2026-11-17T02:23:01.000Z [DEBUG] [bridge:ws] <<< {{"text":"POST /v1/code/sessions/'
        f'{_CSE}/archive"}}\n',
        f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] GET /v1/code/sessions/{_CSE}/archive\n",
        f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] POST /v1/code/sessions/{_CSE}/unarchive\n",
    ],
)
def test_lines_that_only_look_like_an_archive_do_not_count(tmp_path, line):
    path = _log(tmp_path, _REGISTERED + line)
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is False


def test_end_is_seen_on_a_later_read(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    assert tracker.has_ended(path, _SID, _ENV) is False
    with path.open("ab") as fh:
        fh.write(_ARCHIVED.encode())
    assert tracker.has_ended(path, _SID, _ENV) is True


def test_a_line_still_being_written_is_read_whole_later(tmp_path):
    path = _log(tmp_path, _REGISTERED + _ARCHIVED[:40])
    tracker = SessionEndTracker()
    assert tracker.has_ended(path, _SID, _ENV) is False
    with path.open("ab") as fh:
        fh.write(_ARCHIVED[40:].encode())
    assert tracker.has_ended(path, _SID, _ENV) is True


def test_only_new_bytes_are_read_after_the_first_pass(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    tracker.has_ended(path, _SID, _ENV)
    offset = tracker._logs[path].offset
    assert offset == len(_REGISTERED.encode())
    # A byte changed before the offset is never read again, which proves the seek.
    with path.open("r+b") as fh:
        fh.write(b"X")
    tracker.has_ended(path, _SID, _ENV)
    assert tracker._logs[path].offset == offset


def test_a_truncated_log_is_read_from_the_start(tmp_path):
    path = _log(tmp_path, _REGISTERED + "x" * 500 + "\n")
    tracker = SessionEndTracker()
    assert tracker.has_ended(path, _SID, _ENV) is False
    path.write_bytes((_REGISTERED + _ARCHIVED).encode())  # shorter than the old offset
    assert tracker.has_ended(path, _SID, _ENV) is True


def test_no_log_path_cannot_vouch_for_the_session():
    assert SessionEndTracker().has_ended(None, _SID, _ENV) is True


def test_a_missing_log_cannot_vouch_for_the_session(tmp_path):
    assert SessionEndTracker().has_ended(tmp_path / "gone.log", _SID, _ENV) is True


def test_a_log_of_another_environment_cannot_vouch_for_the_session(tmp_path):
    # The newest log of a project can belong to a different bridge in the same folder.
    path = _log(tmp_path, _REGISTERED.replace(_ENV, "env_01OTHERENVAAAAAAAAAAAAAA"))
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is True


def test_no_recorded_environment_skips_the_ownership_check(tmp_path):
    path = _log(tmp_path, "2026-11-17T01:25:00.000Z [DEBUG] [bridge:init] started\n")
    assert SessionEndTracker().has_ended(path, _SID, None) is False


def test_a_transient_read_error_keeps_the_link(tmp_path, monkeypatch):
    path = _log(tmp_path, _REGISTERED + _ARCHIVED)

    def _boom(self, *_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    assert SessionEndTracker().has_ended(path, _SID, _ENV) is False


def test_keep_only_forgets_other_logs(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    tracker.has_ended(path, _SID, _ENV)
    tracker.keep_only({path})
    assert path in tracker._logs
    tracker.keep_only(set())
    assert path not in tracker._logs


def test_session_key_matches_both_forms():
    assert bridge_log.session_key(_SID) == bridge_log.session_key(_CSE)


# ----- the link itself --------------------------------------------------------------


def _row(**kwargs) -> RemoteControlInstance:
    return RemoteControlInstance(
        project="alpha",
        label="alpha",
        environment_id=_ENV,
        url=f"https://claude.ai/code?environment={_ENV}",
        starter_session_id=_SID,
        **kwargs,
    )


def test_a_retired_starter_has_no_session_url():
    row = _row()
    assert row.session_url == f"https://claude.ai/code/{_SID}?from=cli"
    row.retire_starter_session()
    assert row.session_url is None
    assert (row.session_url or row.url) == row.url
    assert row.starter_session_id == _SID  # still known, for exact-value redaction


def test_reseeding_the_same_id_does_not_bring_the_link_back():
    row = _row()
    row.retire_starter_session()
    row.starter_session_id = _SID  # what a reattach from the pointer does
    assert row.session_url is None


def test_a_new_starter_links_again():
    row = _row()
    row.retire_starter_session()
    row.starter_session_id = "session_01NEWSESSIONAAAAAAAAAA"
    assert row.session_url == "https://claude.ai/code/session_01NEWSESSIONAAAAAAAAAA?from=cli"


def test_the_ended_marker_is_not_serialized():
    row = _row()
    row.retire_starter_session()
    dumped = row.model_dump(mode="json")
    assert "_ended_session_id" not in dumped
    assert "ended_session_id" not in dumped
    assert dumped["session_url"] is None
