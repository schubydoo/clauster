"""Which sessions a standard bridge ended, read from its debug log (TASK-557)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clauster import bridge_log
from clauster.models import RemoteControlInstance
from clauster.session_ends import LinkEvidence, SessionEndTracker

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
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED


def test_a_live_session_has_not_ended(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.LIVE


def test_another_sessions_archive_does_not_end_this_one(tmp_path):
    other = _ARCHIVED.replace(_CSE, "cse_01OTHERSESSIONAAAAAAAAAA")
    path = _log(tmp_path, _REGISTERED + other)
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.LIVE


@pytest.mark.parametrize(
    "line",
    [
        # Session text quoted inside a websocket frame must not count.
        f'2026-11-17T02:23:01.000Z [DEBUG] [bridge:ws] <<< {{"text":"POST /v1/code/sessions/'
        f'{_CSE}/archive"}}\n',
        # A frame that quotes the whole log line, prefix included.
        f'2026-11-17T02:23:01.000Z [DEBUG] [bridge:ws] <<< {{"text":"{_ARCHIVED.strip()}"}}\n',
        f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] GET /v1/code/sessions/{_CSE}/archive\n",
        f"2026-11-17T02:23:01.000Z [DEBUG] [bridge:api] POST /v1/code/sessions/{_CSE}/unarchive\n",
    ],
)
def test_lines_that_only_look_like_an_archive_do_not_count(tmp_path, line):
    path = _log(tmp_path, _REGISTERED + line)
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.LIVE


def test_end_is_seen_on_a_later_read(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.LIVE
    with path.open("ab") as fh:
        fh.write(_ARCHIVED.encode())
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED


def test_a_line_still_being_written_is_read_whole_later(tmp_path):
    path = _log(tmp_path, _REGISTERED + _ARCHIVED[:40])
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.LIVE
    with path.open("ab") as fh:
        fh.write(_ARCHIVED[40:].encode())
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED


def test_only_new_bytes_are_read_after_the_first_pass(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    tracker.evidence(path, _SID, _ENV)
    offset = tracker._logs[path].offset
    assert offset == len(_REGISTERED.encode())
    # A byte changed before the offset is never read again, which proves the seek.
    with path.open("r+b") as fh:
        fh.write(b"X")
    tracker.evidence(path, _SID, _ENV)
    assert tracker._logs[path].offset == offset


def test_a_truncated_log_is_read_from_the_start(tmp_path):
    path = _log(tmp_path, _REGISTERED + "x" * 500 + "\n")
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.LIVE
    path.write_bytes((_REGISTERED + _ARCHIVED).encode())  # shorter than the old offset
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED


def test_lines_are_found_across_read_chunks(tmp_path, monkeypatch):
    # A chunk boundary inside the line must neither lose it nor read it twice as two halves.
    monkeypatch.setattr("clauster.session_ends._READ_SIZE", 7)
    path = _log(tmp_path, _REGISTERED + "x\n" + _ARCHIVED)
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED
    assert tracker._logs[path].offset == path.stat().st_size


def test_several_matches_in_one_read_are_all_found(tmp_path):
    other_cse = "cse_01OTHERSESSIONAAAAAAAAAA"
    other_env = "env_01OTHERENVAAAAAAAAAAAAAA"
    path = _log(
        tmp_path,
        _REGISTERED
        + _REGISTERED.replace(_ENV, other_env)
        + _ARCHIVED.replace(_CSE, other_cse)
        + _ARCHIVED,
    )
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED
    assert tracker._logs[path].environments == {_ENV, other_env}
    assert tracker._archived == {
        bridge_log.session_key(_CSE),
        bridge_log.session_key(other_cse),
    }


def test_no_log_path_cannot_vouch_for_the_session():
    assert SessionEndTracker().evidence(None, _SID, _ENV) is LinkEvidence.NO_EVIDENCE


def test_a_missing_log_cannot_vouch_for_the_session(tmp_path):
    missing = tmp_path / "gone.log"
    assert SessionEndTracker().evidence(missing, _SID, _ENV) is LinkEvidence.NO_EVIDENCE


def test_a_log_of_another_environment_cannot_vouch_for_the_session(tmp_path):
    # The newest log of a project can belong to a different bridge in the same folder.
    path = _log(tmp_path, _REGISTERED.replace(_ENV, "env_01OTHERENVAAAAAAAAAAAAAA"))
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.NO_EVIDENCE


def test_an_archive_request_is_proof_in_any_log(tmp_path):
    path = _log(tmp_path, _REGISTERED.replace(_ENV, "env_01OTHERENVAAAAAAAAAAAAAA") + _ARCHIVED)
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED


def test_no_recorded_environment_skips_the_ownership_check(tmp_path):
    path = _log(tmp_path, "2026-11-17T01:25:00.000Z [DEBUG] [bridge:init] started\n")
    assert SessionEndTracker().evidence(path, _SID, None) is LinkEvidence.LIVE


def test_a_transient_read_error_is_reported_as_such(tmp_path, monkeypatch):
    path = _log(tmp_path, _REGISTERED + _ARCHIVED)

    def _boom(self, *_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    assert SessionEndTracker().evidence(path, _SID, _ENV) is LinkEvidence.READ_ERROR


def test_keep_only_forgets_other_logs(tmp_path):
    path = _log(tmp_path, _REGISTERED)
    tracker = SessionEndTracker()
    tracker.evidence(path, _SID, _ENV)
    tracker.keep_only({path})
    assert path in tracker._logs
    tracker.keep_only(set())
    assert path not in tracker._logs


def test_a_proven_archive_outlives_its_log(tmp_path):
    # A later bridge's log says nothing about the session, but the pointer can seed its id.
    old = _log(tmp_path, _REGISTERED + _ARCHIVED)
    new = tmp_path / "alpha-2-1.log"
    new.write_bytes(_REGISTERED.encode())
    tracker = SessionEndTracker()
    assert tracker.evidence(old, _SID, _ENV) is LinkEvidence.ARCHIVED
    tracker.keep_only({new})
    assert old not in tracker._logs
    assert tracker.evidence(new, _SID, _ENV) is LinkEvidence.ARCHIVED
    assert tracker.evidence(None, _SID, _ENV) is LinkEvidence.ARCHIVED
    new.unlink()
    assert tracker.evidence(new, _SID, _ENV) is LinkEvidence.ARCHIVED
    assert tracker.evidence(new, "session_01OTHER", _ENV) is LinkEvidence.NO_EVIDENCE


def test_a_proven_archive_survives_a_read_error(tmp_path, monkeypatch):
    path = _log(tmp_path, _REGISTERED + _ARCHIVED)
    tracker = SessionEndTracker()
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED

    def _boom(self, *_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    assert tracker.evidence(path, _SID, _ENV) is LinkEvidence.ARCHIVED
    assert tracker.evidence(path, "session_01OTHER", _ENV) is LinkEvidence.READ_ERROR


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


def test_an_archived_starter_has_no_session_url():
    row = _row()
    assert row.session_url == f"https://claude.ai/code/{_SID}?from=cli"
    assert row.starter_archived is False
    row.mark_starter_archived()
    assert row.starter_archived is True
    assert row.session_url is None
    assert (row.session_url or row.url) == row.url
    assert row.starter_session_id == _SID  # still known, for exact-value redaction


def test_an_unvouched_starter_can_link_again():
    row = _row()
    row.set_starter_vouched(False)
    assert row.session_url is None
    assert row.starter_archived is False
    row.set_starter_vouched(True)
    assert row.session_url is not None


def test_reseeding_the_same_id_does_not_bring_the_link_back():
    row = _row()
    row.mark_starter_archived()
    row.starter_session_id = _SID  # what a reattach from the pointer does
    assert row.session_url is None


def test_a_new_starter_links_again():
    row = _row()
    row.mark_starter_archived()
    row.starter_session_id = "session_01NEWSESSIONAAAAAAAAAA"
    assert row.starter_archived is False
    assert row.session_url == "https://claude.ai/code/session_01NEWSESSIONAAAAAAAAAA?from=cli"


def test_the_link_markers_are_not_serialized():
    row = _row()
    row.mark_starter_archived()
    dumped = row.model_dump(mode="json")
    assert not [key for key in dumped if "archived" in key or "unvouched" in key]
    assert dumped["session_url"] is None
