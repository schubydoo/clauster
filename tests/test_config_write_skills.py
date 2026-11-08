"""Skills config-write surface (#691) over the #347 Foundation + #766 file/dir writer.

Covers the structural validators (SKILL.md frontmatter shape, script bodies treated
as OPAQUE blobs — never parsed/resolved/executed), the directory writer (add/replace/
delete via the #766 file/dir-writer primitive), the EXTRA script-body confirm gate,
containment (invalid names, ``../`` member paths, symlink escape), the read-redaction
decision (skill content IS redacted, unlike CLAUDE.md), the plugin-read-only guard
(content-marker rejection), and the ``skillOverrides`` settings-key surface at all
three scopes (user/project/local).

Every test that touches ``~/.claude`` runs under the autouse HOME-isolation fixture
and writes only into the isolated tmp home — the live account is never touched.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from httpx import Response

from clauster import config_write as cw
from clauster import config_write_skills as sk
from clauster.app import create_app
from clauster.config import load_config
from conftest import needs_symlink

_VALID_MD = "---\ndescription: does a thing\n---\nInstructions here.\n"


def _md(description: str = "does a thing", **extra: object) -> str:
    lines = ["---", f"description: {description}"]
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("Body text.")
    return "\n".join(lines) + "\n"


# --- name validation -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["my-skill", "a", "A1_2-3", "x" * 64])
def test_valid_skill_names_accepted(name: str) -> None:
    assert sk.is_valid_skill_name(name)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "-leading", "has/slash", "has..dot", "trailing.", "has space", "x" * 65],
)
def test_invalid_skill_names_rejected(name: str) -> None:
    assert not sk.is_valid_skill_name(name)


# --- frontmatter parsing ---------------------------------------------------------------


def test_parse_frontmatter_round_trips() -> None:
    frontmatter, body = sk.parse_frontmatter(_VALID_MD)
    assert frontmatter == {"description": "does a thing"}
    assert body.strip() == "Instructions here."


@pytest.mark.parametrize(
    "content",
    [
        "no frontmatter at all",
        "---\nmissing closing marker\n",
        "---\n: not: valid: yaml: [\n---\nbody",
        "---\n- a\n- list\n---\nbody",  # frontmatter is a list, not a mapping
    ],
)
def test_parse_frontmatter_rejects_bad_shape(content: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        frontmatter, _body = sk.parse_frontmatter(content)
        sk.validate_frontmatter(frontmatter)


def test_parse_frontmatter_rejects_deeply_nested_yaml() -> None:
    # Deeply-nested flow sequences overflow PyYAML's recursive composer, which raises
    # RecursionError — not a YAMLError — so it escaped the handler and this
    # code-executing write tier raised outside its documented InvalidCandidateError
    # contract. Kept in lockstep with the subagents parser (same test, same tier).
    deep = "[" * 5_000 + "]" * 5_000
    with pytest.raises(cw.InvalidCandidateError, match="too deeply"):
        sk.parse_frontmatter(f"---\ndescription: {deep}\n---\nbody\n")


@pytest.mark.parametrize(
    ("tag", "raised"),
    [
        ("!!int", ValueError),
        ("!!float", ValueError),
        ("!!bool", KeyError),
        ("!!timestamp", AttributeError),
    ],
)
def test_parse_frontmatter_rejects_explicit_tag_with_an_unfitting_value(
    tag: str, raised: type[Exception]
) -> None:
    # #1354: PyYAML's SafeConstructor raises these four OUTSIDE yaml.YAMLError for a
    # resolvable explicit tag whose value does not fit it, so each escaped the documented
    # InvalidCandidateError contract — a 500 on this code-executing write tier, from a
    # SKILL.md that arrived with a cloned repository. The input is the fuzzer's;
    # `__cause__` pins WHICH class was caught. Kept in lockstep with the subagents parser
    # (one shared handler, config_write.load_frontmatter_yaml).
    with pytest.raises(cw.InvalidCandidateError, match="YAML tag") as excinfo:
        sk.parse_frontmatter(f"---\nname: {tag} x\ndescription: d\n---\nbody\n")
    assert isinstance(excinfo.value.__cause__, raised)


@pytest.mark.parametrize("value", ["!!int", "!!float", "!!int __"])
def test_parse_frontmatter_rejects_explicit_int_tag_with_an_empty_scalar(value: str) -> None:
    # Review catch on the first fix for issue 1354: construct_yaml_int/float INDEX the
    # scalar before parsing it, so an empty (or all-underscore, which strips to empty)
    # scalar raises IndexError — none of the unfitting-value trio above — and still
    # escaped as a 500 until IndexError joined the shared handler's tuple.
    with pytest.raises(cw.InvalidCandidateError, match="YAML tag") as excinfo:
        sk.parse_frontmatter(f"---\nname: {value}\ndescription: d\n---\nbody\n")
    assert isinstance(excinfo.value.__cause__, IndexError)


def test_parse_frontmatter_tag_rejection_does_not_echo_the_value() -> None:
    # Invariant 4. PyYAML embeds the offending scalar in all four of these exceptions
    # ("KeyError('<the value>')"), and list_skills surfaces this message to the browser as
    # `frontmatter_error`. redact_secret_lines cannot save it: its key/value scanner is
    # line-anchored, so a payload sitting mid-message is unreachable. So the handler names
    # the exception CLASS and never its payload. The value below is low-entropy padding on
    # purpose (a secret-shaped literal in any commit fails the gitleaks gate).
    secret = "FAKEFAKEFAKEFAKEFAKEfake42"
    with pytest.raises(cw.InvalidCandidateError) as excinfo:
        sk.parse_frontmatter(f"---\napi_key: !!bool {secret}\n---\nbody\n")
    message = str(excinfo.value)
    assert secret.lower() not in message.lower()
    assert secret.lower() not in cw.redact_secret_lines(message).lower()
    assert "KeyError" in message


def test_parse_frontmatter_tolerates_trailing_whitespace_on_a_fence() -> None:
    # #1352: this parser used to REJECT a fence carrying a trailing space while the
    # subagents parser accepted it, so the same file parsed on one surface of the write
    # tier and 422'd on the other. Both now alias one pattern; the cross-parser assertion
    # lives in tests/test_fuzz_harness_smoke.py.
    frontmatter, body = sk.parse_frontmatter("--- \ndescription: y\n---\t\nbody\n")
    assert frontmatter == {"description": "y"}
    assert body == "body\n"


def test_validate_frontmatter_requires_no_field() -> None:
    # TASK-449.6: "All fields are optional" (docs, frontmatter reference). Without a
    # description the CLI uses the first line of the content. Before this the surface
    # required `description`.
    assert sk.validate_frontmatter({}) == {}


def test_validate_frontmatter_passes_unknown_keys_through() -> None:
    # #958/DF-3: Claude Code tolerates forward-compatible SKILL.md frontmatter keys, so
    # a valid skill carrying keys clauster has no opinion about (effort/license/metadata)
    # must NOT be rejected as "unknown" — only the recognized keys are type-checked.
    sk.validate_frontmatter(
        {"description": "x", "effort": "high", "license": "MIT", "metadata": {"a": 1}}
    )  # no raise


def test_validate_frontmatter_accepts_all_documented_fields() -> None:
    sk.validate_frontmatter(
        {
            "name": "my-skill",
            "description": "x",
            "disable-model-invocation": True,
            "user-invocable": False,
            "allowed-tools": "Read Grep",
            "argument-hint": "<path>",
        }
    )


@pytest.mark.parametrize(
    "candidate",
    [
        {"description": "x", "name": "a<b"},
        {"description": "x", "name": "two  spaces"},
        {"description": "x", "disable-model-invocation": "maybe"},
        {"description": "x", "user-invocable": "maybe"},
        {"description": "x", "allowed-tools": 5},
        {"description": "x", "argument-hint": {"a": 1}},
        {"description": ""},
        {"description": "   "},
        {"description": [5]},
        "not-a-dict",
    ],
)
def test_validate_frontmatter_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_frontmatter(candidate)


# --- validate_skill_md_content ---------------------------------------------------------


def test_validate_skill_md_content_accepts_valid() -> None:
    sk.validate_skill_md_content(_VALID_MD)  # no raise


def test_validate_skill_md_content_rejects_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_md_content(123)


def test_validate_skill_md_content_rejects_oversize() -> None:
    huge = _md(description="x" * (sk.MAX_SKILL_MD_BYTES + 1))
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_md_content(huge)


@pytest.mark.parametrize(
    "content",
    [
        "---\ndescription: x\nscript: ${CLAUDE_PLUGIN_ROOT}/y.sh\n---\nbody",
        "---\ndescription: x\n---\n${CLAUDE_PLUGIN_ROOT}/y.sh",
    ],
)
def test_validate_skill_md_content_rejects_plugin_marker(content: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        sk.validate_skill_md_content(content)


# --- validate_script_body ---------------------------------------------------------------


def test_validate_script_body_accepts_arbitrary_text() -> None:
    sk.validate_script_body("#!/bin/sh\nrm -rf /\n", "scripts/x.sh")  # no raise -- opaque


def test_validate_script_body_rejects_non_string() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_script_body(123, "scripts/x.sh")


def test_validate_script_body_rejects_oversize() -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_script_body("x" * (sk.MAX_FILE_BYTES + 1), "scripts/x.sh")


def test_validate_script_body_rejects_plugin_marker() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="plugin"):
        sk.validate_script_body("echo ${CLAUDE_PLUGIN_ROOT}/x", "scripts/x.sh")


def test_validate_script_body_never_executes_the_content(tmp_path: Path) -> None:
    """RCE-NEGATIVE: validating a script that WOULD create a marker must not run it."""
    marker = tmp_path / "PWNED_SCRIPT_VALIDATE"
    pwn = f"touch {shlex.quote(str(marker))}"
    sk.validate_script_body(pwn, "scripts/x.sh")
    assert not marker.exists()


# --- write_skill: basic add/replace, requires SKILL.md ----------------------------------


def test_write_skill_creates_directory(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    target = tmp_path / "skills" / "my-skill" / sk.SKILL_FILENAME
    assert target.read_text(encoding="utf-8") == _VALID_MD


def test_write_skill_requires_skill_md_key(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="SKILL.md"):
        sk.write_skill(tmp_path, "my-skill", {"other.md": "x"}, expected_hash=None)
    assert not (tmp_path / "skills").exists()


def test_write_skill_rejects_empty_files(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.write_skill(tmp_path, "my-skill", {}, expected_hash=None)


def test_write_skill_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(cw.PathEscapeError):
        sk.write_skill(tmp_path, "../escape", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    assert not (tmp_path / "skills").exists()


def test_write_skill_rejects_bad_skill_md_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.write_skill(
            tmp_path, "my-skill", {sk.SKILL_FILENAME: "---\nnever closed\n"}, expected_hash=None
        )
    assert not (tmp_path / "skills").exists()


def test_write_skill_replace_round_trip(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    h = sk._skill_md_hash(tmp_path / "skills", "my-skill")
    updated = _md(description="updated")
    sk.write_skill(
        tmp_path,
        "my-skill",
        {sk.SKILL_FILENAME: updated},
        expected_hash=h,
        confirm_scripts=None,
    )
    target = tmp_path / "skills" / "my-skill" / sk.SKILL_FILENAME
    assert target.read_text(encoding="utf-8") == updated


def test_write_skill_stale_hash_raises(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    with pytest.raises(cw.StaleConfigWriteError):
        sk.write_skill(
            tmp_path,
            "my-skill",
            {sk.SKILL_FILENAME: _md(description="x")},
            expected_hash=cw.hash_bytes(b"stale"),
        )


def test_write_skill_no_hash_on_existing_is_stale(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    with pytest.raises(cw.StaleConfigWriteError):
        sk.write_skill(
            tmp_path, "my-skill", {sk.SKILL_FILENAME: _md(description="y")}, expected_hash=None
        )


def test_write_skill_total_size_cap(tmp_path: Path) -> None:
    files = {
        sk.SKILL_FILENAME: _VALID_MD,
        "scripts/big.sh": "x" * (sk.MAX_TOTAL_BYTES),
    }
    with pytest.raises(cw.InvalidCandidateError, match="byte cap"):
        sk.write_skill(
            tmp_path,
            "my-skill",
            files,
            expected_hash=None,
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )


# --- EXTRA script-body confirm gate ------------------------------------------------------


def test_write_skill_extra_file_requires_script_confirm(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": "#!/bin/sh\necho hi\n"}
    with pytest.raises(sk.ScriptConfirmRequiredError):
        sk.write_skill(tmp_path, "my-skill", files, expected_hash=None)
    assert not (tmp_path / "skills").exists()


def test_write_skill_extra_file_rejects_wrong_confirm_token(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": "echo hi"}
    with pytest.raises(sk.ScriptConfirmRequiredError):
        sk.write_skill(tmp_path, "my-skill", files, expected_hash=None, confirm_scripts="yes")
    assert not (tmp_path / "skills").exists()


def test_write_skill_extra_file_with_exact_token_succeeds(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": "echo hi"}
    sk.write_skill(
        tmp_path, "my-skill", files, expected_hash=None, confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN
    )
    assert (tmp_path / "skills" / "my-skill" / "scripts" / "x.sh").read_text() == "echo hi"


def test_write_skill_skill_md_only_needs_no_script_confirm(tmp_path: Path) -> None:
    # Uploading ONLY SKILL.md never needs the extra confirm.
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)


def test_write_skill_never_executes_the_uploaded_script(tmp_path: Path) -> None:
    """RCE-NEGATIVE: writing a skill whose script WOULD create a marker must not run it."""
    marker = tmp_path / "PWNED_SCRIPT_WRITE"
    pwn = f"touch {shlex.quote(str(marker))}"
    files = {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": pwn}
    sk.write_skill(
        tmp_path, "my-skill", files, expected_hash=None, confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN
    )
    assert not marker.exists()
    stored = (tmp_path / "skills" / "my-skill" / "scripts" / "x.sh").read_text()
    assert stored == pwn


# --- containment: invalid member paths, symlink escape -----------------------------------


def test_write_skill_rejects_dotdot_member_path(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: _VALID_MD, "../escape.sh": "x"}
    with pytest.raises(cw.PathEscapeError):
        sk.write_skill(
            tmp_path,
            "my-skill",
            files,
            expected_hash=None,
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    # Nothing was promoted -- the escaping member aborts the whole build.
    assert not (tmp_path / "skills" / "my-skill").exists()


def test_write_skill_rejects_absolute_member_path(tmp_path: Path) -> None:
    files = {sk.SKILL_FILENAME: _VALID_MD, "/etc/passwd": "x"}
    with pytest.raises(cw.PathEscapeError):
        sk.write_skill(
            tmp_path,
            "my-skill",
            files,
            expected_hash=None,
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )


@needs_symlink
def test_read_skill_file_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"top secret\n")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(_VALID_MD.encode("utf-8"))
    (skill_dir / "escape.txt").symlink_to(outside)
    with pytest.raises(cw.PathEscapeError):
        sk.read_skill_file(tmp_path, "my-skill", "escape.txt")


@needs_symlink
def test_list_skills_does_not_read_symlinked_skill_md(tmp_path: Path) -> None:
    # A symlinked SKILL.md pointing at an outside secret must NOT be followed: the
    # skill lists as has_skill_md=False and the target content never surfaces
    # (no description, no frontmatter_error quoting it).
    outside = tmp_path / "outside_secret_skill.md"
    outside.write_bytes(b"---\ndescription: TOPSECRET_LEAKED_VALUE\n---\nbody\n")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).symlink_to(outside)
    listing = sk.list_skills(tmp_path)
    assert listing == [{"name": "my-skill", "has_skill_md": False, "files": []}]
    # Belt-and-suspenders: the secret never appears anywhere in the serialized listing.
    assert "TOPSECRET_LEAKED_VALUE" not in repr(listing)


@needs_symlink
def test_list_skills_does_not_enumerate_symlinked_member(tmp_path: Path) -> None:
    # A member symlink targeting an outside file must NOT be enumerated in `files`
    # (so it can never be offered up for a later read that would leak the target).
    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"top secret\n")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(_VALID_MD.encode("utf-8"))
    (skill_dir / "real.sh").write_bytes(b"echo hi\n")
    (skill_dir / "escape.txt").symlink_to(outside)
    listing = sk.list_skills(tmp_path)
    assert listing[0]["files"] == ["real.sh"]  # symlinked member skipped


@needs_symlink
def test_list_skills_skips_member_under_symlinked_subdir(tmp_path: Path) -> None:
    # A regular file is not itself a symlink, but if it lives under a symlinked
    # subdir that escapes the skill dir, its resolved path escapes and it is skipped.
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_bytes(b"secret\n")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(_VALID_MD.encode("utf-8"))
    (skill_dir / "sub").symlink_to(outside_dir, target_is_directory=True)
    listing = sk.list_skills(tmp_path)
    assert listing[0]["files"] == []  # nothing under the escaping symlinked subdir


@needs_symlink
def test_delete_skill_rejects_symlinked_skill_name(tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "sensitive.txt").write_text("data")
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "sneaky").symlink_to(outside)
    with pytest.raises(cw.PathEscapeError):
        sk.delete_skill(tmp_path, "sneaky")
    # The outside directory must survive untouched.
    assert (outside / "sensitive.txt").exists()


# --- delete_skill --------------------------------------------------------------------------


def test_delete_skill_removes_directory(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    assert sk.delete_skill(tmp_path, "my-skill") is True
    assert not (tmp_path / "skills" / "my-skill").exists()


def test_delete_skill_missing_returns_false(tmp_path: Path) -> None:
    assert sk.delete_skill(tmp_path, "never-existed") is False


def test_delete_skill_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(cw.PathEscapeError):
        sk.delete_skill(tmp_path, "../escape")


def test_skill_md_hash_rejects_invalid_name(tmp_path: Path) -> None:
    # Direct unit test of the private helper's own containment guard -- every public
    # caller already validates the name first, so this exercises the defensive branch.
    with pytest.raises(cw.PathEscapeError):
        sk._skill_md_hash(tmp_path / "skills", "../escape")


def test_write_skill_rejects_non_string_content_among_multiple_files(tmp_path: Path) -> None:
    # The total-size accumulation loop type-checks EVERY file's content up front,
    # before either validate_skill_md_content or validate_script_body runs.
    files = {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": 12345}
    with pytest.raises(cw.InvalidCandidateError, match="must be a string"):
        sk.write_skill(
            tmp_path,
            "my-skill",
            files,
            expected_hash=None,
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )


# --- list_skills ---------------------------------------------------------------------------


def test_list_skills_empty_when_absent(tmp_path: Path) -> None:
    assert sk.list_skills(tmp_path) == []


def test_list_skills_reports_valid_skill(tmp_path: Path) -> None:
    sk.write_skill(
        tmp_path,
        "my-skill",
        {sk.SKILL_FILENAME: _md(description="does things", **{"disable-model-invocation": True})},
        expected_hash=None,
    )
    listing = sk.list_skills(tmp_path)
    assert len(listing) == 1
    item = listing[0]
    assert item["name"] == "my-skill"
    assert item["has_skill_md"] is True
    assert item["description"] == "does things"
    assert item["disable_model_invocation"] is True
    assert item["files"] == []


def test_list_skills_redacts_secret_in_description(tmp_path: Path) -> None:
    # A secret pasted into a description: line must be masked in the LIST metadata,
    # exactly as it is on the file-body read view -- the redaction invariant holds
    # on every read path, not just read_skill_file.
    secretish = "connects to slack://XOXB-super-secret-token@hooks"
    sk.write_skill(
        tmp_path, "my-skill", {sk.SKILL_FILENAME: _md(description=secretish)}, expected_hash=None
    )
    listing = sk.list_skills(tmp_path)
    assert "XOXB-super-secret-token" not in listing[0]["description"]
    assert cw.REDACTION_SENTINEL in listing[0]["description"]


def test_list_skills_frontmatter_error_never_leaks_a_token_from_yaml_prose(
    tmp_path: Path,
) -> None:
    # #1369 end to end, at the surface the issue names. PyYAML writes the offending token
    # into the message PROSE for an undefined alias / duplicate anchor / unknown tag, where it
    # sits mid-line with no `key:` anchor -- so `redact_secret_lines`, whose scanner is
    # line-anchored, structurally cannot mask it and a credential pasted into frontmatter
    # reached the dashboard. Low-entropy padding, per the convention above.
    token = "FAKEFAKEFAKEFAKEFAKEfake42"
    skills_root = tmp_path / "skills"
    for i, header in enumerate(
        # Nested: the CLI retry re-quotes a top-level line with `*` or `&` (TASK-449.6).
        (
            f"name: x\nm:\n  extra: *{token}\n",
            f"m:\n  a: &{token} 1\n  b: &{token} 2\n",
            f"a: !{token} 1\n",
        )
    ):
        bad = skills_root / f"broken{i}"
        bad.mkdir(parents=True)
        (bad / sk.SKILL_FILENAME).write_text(f"---\n{header}---\nbody\n")

    listing = sk.list_skills(tmp_path)

    assert len(listing) == 3
    for item in listing:
        err = item["frontmatter_error"]
        assert token.lower() not in err.lower(), err
        assert "line " in err and "column " in err, err  # ...and it still says where


def test_list_skills_frontmatter_error_line_matches_the_file_not_the_header(
    tmp_path: Path,
) -> None:
    # The line number is now the ONLY locator the operator gets, so it has to be the line in
    # the FILE. `parse_frontmatter` hands the shared seam `FRONTMATTER_RE.group(1)`, which
    # begins after the opening `---` fence, so a raw PyYAML mark names the line one ABOVE the
    # fault. The tab below is on file line 3; without `line_offset=1` the message said 2.
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    (bad / sk.SKILL_FILENAME).write_text("---\nname: x\n\tbad: 1\n---\nbody\n")

    err = sk.list_skills(tmp_path)[0]["frontmatter_error"]

    assert "line 3, column 1" in err, err


def test_list_skills_frontmatter_error_carries_no_document_text(tmp_path: Path) -> None:
    # A YAML parse error used to echo a fragment of the offending line back, and
    # `redact_secret_lines` masked the `${...}` interpolation in it -- so this asserted both
    # "secret absent" and "sentinel present". #1369 stopped interpolating the exception at
    # all: the message is now built from the error's POSITIONS only, so no document text
    # reaches the browser and there is nothing left to mask. The secret-absent half is
    # therefore stronger than it was; the sentinel half no longer applies.
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    (bad / sk.SKILL_FILENAME).write_bytes(
        # Nested: the CLI retry would re-quote a top-level `[` line (TASK-449.6).
        b"---\nm:\n  description: [unclosed ${SUPER_SECRET_VALUE}\n---\nbody"
    )
    listing = sk.list_skills(tmp_path)
    err = listing[0]["frontmatter_error"]
    assert "SUPER_SECRET_VALUE" not in err
    assert "unclosed" not in err  # no quoted source line at all, masked or otherwise
    assert "line " in err and "column " in err  # ...but it still says where to look


def test_list_skills_reports_frontmatter_error_without_raising(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    (bad / sk.SKILL_FILENAME).write_bytes(b"---\nan unclosed frontmatter block\n")
    listing = sk.list_skills(tmp_path)
    assert len(listing) == 1
    assert "frontmatter_error" in listing[0]


def test_list_skills_includes_supporting_files(tmp_path: Path) -> None:
    sk.write_skill(
        tmp_path,
        "my-skill",
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": "echo hi"},
        expected_hash=None,
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    listing = sk.list_skills(tmp_path)
    assert listing[0]["files"] == ["scripts/x.sh"]


def test_list_skills_excludes_member_when_resolve_oserror(tmp_path: Path, monkeypatch) -> None:
    # A regular member whose resolve() raises OSError (a racing IO/permission fault)
    # fails closed in _is_contained_regular_file: it is silently excluded from `files`
    # rather than surfaced or raised -- it can never be offered for a later read.
    sk.write_skill(
        tmp_path,
        "my-skill",
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/x.sh": "echo hi"},
        expected_hash=None,
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    real_resolve = Path.resolve

    def _boom(self, *a, **k):
        if self.name == "x.sh":  # only the one member file's resolve faults
            raise OSError("resolve failed")
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", _boom)
    listing = sk.list_skills(tmp_path)
    assert listing[0]["files"] == []  # the un-resolvable member is dropped


def test_list_skills_files_use_posix_separators(tmp_path: Path) -> None:
    # A nested member key is always forward-slash, on every OS (logical member keys,
    # not host filesystem paths — no backslash even on Windows).
    sk.write_skill(
        tmp_path,
        "my-skill",
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/deep/run.sh": "echo hi"},
        expected_hash=None,
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    files = sk.list_skills(tmp_path)[0]["files"]
    assert files == ["scripts/deep/run.sh"]
    assert all("\\" not in f for f in files)


def test_list_skills_skips_invalid_names(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "..bad..").mkdir()
    assert sk.list_skills(tmp_path) == []


def test_list_skills_reports_missing_skill_md(tmp_path: Path) -> None:
    # A skill directory that exists but has no SKILL.md yet (e.g. a half-finished
    # upload) is still listed, just with has_skill_md=False and no description.
    skills_root = tmp_path / "skills"
    (skills_root / "empty-skill").mkdir(parents=True)
    listing = sk.list_skills(tmp_path)
    assert listing == [{"name": "empty-skill", "has_skill_md": False, "files": []}]


def test_list_skills_reports_non_utf8_skill_md(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    (bad / sk.SKILL_FILENAME).write_bytes(b"\xff\xfe not utf-8")
    listing = sk.list_skills(tmp_path)
    assert listing[0]["frontmatter_error"] == f"{sk.SKILL_FILENAME} is not valid UTF-8"


# --- read_skill_file: redaction, missing file -----------------------------------------------


def test_read_skill_file_missing_skill_is_empty_not_exists(tmp_path: Path) -> None:
    content, file_hash, exists = sk.read_skill_file(tmp_path, "absent")
    assert content == ""
    assert exists is False
    assert file_hash == cw.hash_bytes(b"")


def test_read_skill_file_returns_content_and_hash(tmp_path: Path) -> None:
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    content, file_hash, exists = sk.read_skill_file(tmp_path, "my-skill")
    assert content == _VALID_MD
    assert exists is True
    assert file_hash == cw.hash_bytes(_VALID_MD.encode("utf-8"))


def test_read_skill_file_redacts_secret_shaped_lines(tmp_path: Path) -> None:
    """The #813 INFO-1 gap this surface closes: a lone credential line IS masked."""
    content = _VALID_MD + "\napi_key: sk-super-secret-value\n"
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: content}, expected_hash=None)
    read_back, _h, _exists = sk.read_skill_file(tmp_path, "my-skill")
    assert "sk-super-secret-value" not in read_back
    assert cw.REDACTION_SENTINEL in read_back


def test_read_skill_file_hash_matches_returned_content_bytes(tmp_path: Path) -> None:
    # Single-read invariant (TOCTOU fix): for a non-secret file the returned content
    # is byte-identical to what the returned hash describes -- both derive from the
    # SAME read_bytes, so hash == sha256(returned content bytes).
    body = "---\ndescription: plain non-secret skill\n---\njust instructions\n"
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: body}, expected_hash=None)
    content, file_hash, exists = sk.read_skill_file(tmp_path, "my-skill")
    assert exists is True
    assert content == body  # no redaction happened (nothing secret-shaped)
    assert file_hash == cw.hash_bytes(content.encode("utf-8"))
    # And the hash the reader returns is exactly the one a follow-up write must echo.
    updated = "---\ndescription: plain non-secret skill v2\n---\nmore\n"
    sk.write_skill(tmp_path, "my-skill", {sk.SKILL_FILENAME: updated}, expected_hash=file_hash)


def test_read_skill_file_rejects_invalid_name(tmp_path: Path) -> None:
    with pytest.raises(cw.PathEscapeError):
        sk.read_skill_file(tmp_path, "../escape")


def test_read_skill_file_rejects_non_utf8(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(cw.InvalidCandidateError):
        sk.read_skill_file(tmp_path, "my-skill")


# --- project/user scope wrappers ------------------------------------------------------------


def test_project_scope_round_trip(tmp_path: Path) -> None:
    sk.write_project_skill(
        tmp_path, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None
    )
    listing = sk.list_project_skills(tmp_path)
    assert listing[0]["name"] == "my-skill"
    content, _h, exists = sk.read_project_skill_file(tmp_path, "my-skill")
    assert exists is True
    assert content == _VALID_MD
    assert sk.delete_project_skill(tmp_path, "my-skill") is True


def test_user_scope_round_trip(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    sk.write_user_skill(
        claude_json, "my-skill", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None
    )
    listing = sk.list_user_skills(claude_json)
    assert listing[0]["name"] == "my-skill"
    content, _h, exists = sk.read_user_skill_file(claude_json, "my-skill")
    assert exists is True
    assert sk.delete_user_skill(claude_json, "my-skill") is True
    assert (tmp_path / ".claude" / "skills").exists()  # base dir untouched by delete


# --- skillOverrides validator -----------------------------------------------------------


def test_validate_skill_overrides_accepts_all_values() -> None:
    sk.validate_skill_overrides(
        {"a": "on", "b": "name-only", "c": "user-invocable-only", "d": "off"}
    )


def test_validate_skill_overrides_accepts_empty() -> None:
    sk.validate_skill_overrides({})


@pytest.mark.parametrize(
    "candidate",
    [
        "not-a-dict",
        {"": "on"},
        {"two  spaces": "on"},
        {"__proto__": "on"},
        {"good-name": True},  # another STRING warns (TASK-524)
        {"good-name": ["off"]},  # unhashable: must not leak a bare TypeError
        {"good-name": {}},
        {5: "on"},
    ],
)
def test_validate_skill_overrides_rejects_bad_shape(candidate: object) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_overrides(candidate)


def test_project_overrides_round_trip(tmp_path: Path) -> None:
    overrides, h0 = sk.read_project_skill_overrides(tmp_path)
    assert overrides == {}
    sk.write_project_skill_overrides(tmp_path, {"my-skill": "off"}, expected_hash=h0)
    overrides, _h1 = sk.read_project_skill_overrides(tmp_path)
    assert overrides == {"my-skill": "off"}


def test_project_overrides_preserves_sibling_keys(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"skillOverrides": {"a": "off"}, "model": "opus"}))
    _o, h = sk.read_project_skill_overrides(tmp_path)
    sk.write_project_skill_overrides(tmp_path, {"b": "name-only"}, h)
    out = json.loads(settings.read_text(encoding="utf-8"))
    assert out["model"] == "opus"
    assert out["skillOverrides"] == {"b": "name-only"}


def test_project_overrides_stale_hash_raises(tmp_path: Path) -> None:
    settings = cw.project_settings_path(tmp_path)
    settings.parent.mkdir(parents=True)
    settings.write_text('{"skillOverrides": {}}')
    with pytest.raises(cw.StaleConfigWriteError):
        sk.write_project_skill_overrides(tmp_path, {"a": "off"}, cw.hash_bytes(b"stale"))


def test_user_overrides_round_trip(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    overrides, h0 = sk.read_user_skill_overrides(settings)
    assert overrides == {}
    sk.write_user_skill_overrides(settings, {"my-skill": "on"}, expected_hash=h0)
    overrides, _h1 = sk.read_user_skill_overrides(settings)
    assert overrides == {"my-skill": "on"}


def test_local_overrides_round_trip_and_gitignore(tmp_path: Path) -> None:
    overrides, h0 = sk.read_project_local_skill_overrides(tmp_path)
    assert overrides == {}
    sk.write_project_local_skill_overrides(tmp_path, {"my-skill": "off"}, expected_hash=h0)
    overrides, _h1 = sk.read_project_local_skill_overrides(tmp_path)
    assert overrides == {"my-skill": "off"}
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_local_overrides_independent_of_project_scope(tmp_path: Path) -> None:
    sk.write_project_skill_overrides(tmp_path, {"a": "off"}, expected_hash=None)
    sk.write_project_local_skill_overrides(tmp_path, {"a": "on"}, expected_hash=None)
    project_o, _ = sk.read_project_skill_overrides(tmp_path)
    local_o, _ = sk.read_project_local_skill_overrides(tmp_path)
    assert project_o == {"a": "off"}
    assert local_o == {"a": "on"}


# --- gated routes (full FastAPI lifespan) -----------------------------------------------

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"


def _client(write_config, tmp_path, extra: str) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{extra}")
    return TestClient(create_app(load_config(cfg)))


_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_PROJECT_ONLY = "config_write:\n  enabled: true\n"

_SKILLS_URL = "/api/config-write/skills"
_FILE_URL = "/api/config-write/skills/file"
_DELETE_URL = "/api/config-write/skills/delete"
_OVERRIDES_URL = "/api/config-write/skills/overrides"


def test_route_skills_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_SKILLS_URL}?project=alpha").status_code == 404
        assert (
            c.put(
                _SKILLS_URL,
                json={
                    "scope": "project",
                    "project": "alpha",
                    "confirm": "alpha",
                    "name": "s",
                    "files": {"SKILL.md": _VALID_MD},
                },
            ).status_code
            == 404
        )


def test_route_skills_local_scope_is_422_not_supported(write_config, tmp_path) -> None:
    # Skill DIRECTORY ops have no local scope (see module docstring) -- unlike
    # skillOverrides below, "local" is simply not a valid scope value here.
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_SKILLS_URL}?scope=local&project=alpha")
        assert resp.status_code == 422


def test_route_skills_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_SKILLS_URL}?scope=bogus").status_code == 422
        assert c.put(_SKILLS_URL, json={"scope": "bogus"}).status_code == 422


def test_route_skills_list_write_read_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        list0 = c.get(f"{_SKILLS_URL}?project=alpha")
        assert list0.status_code == 200
        assert list0.json()["skills"] == []
        wr = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert wr.status_code == 200
        list1 = c.get(f"{_SKILLS_URL}?project=alpha")
        assert list1.json()["skills"][0]["name"] == "greeter"
        view = c.get(f"{_FILE_URL}?project=alpha&name=greeter")
        assert view.status_code == 200
        assert view.json()["content"] == _VALID_MD


def test_route_skills_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert resp.status_code == 400


def test_route_skills_bad_shape_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": "---\nnever closed\n"},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "skills").exists()


def test_route_skills_missing_files_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "name": "greeter"},
        )
        assert resp.status_code == 422


def test_route_skills_missing_name_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert resp.status_code == 422


def test_route_skills_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _md(description="new")},
                "hash": cw.hash_bytes(b"stale"),
            },
        )
        assert resp.status_code == 409


def test_route_skills_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "../escape",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert resp.status_code == 400


def test_route_skills_script_confirm_required_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD, "scripts/x.sh": "echo hi"},
            },
        )
        assert resp.status_code == 400
    assert not (projects_root / "alpha" / ".claude" / "skills").exists()


def test_route_skills_script_confirm_with_token_succeeds(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD, "scripts/x.sh": "echo hi"},
                "confirm_scripts": sk.SCRIPT_CONFIRM_TOKEN,
            },
        )
        assert resp.status_code == 200
    assert (
        projects_root / "alpha" / ".claude" / "skills" / "greeter" / "scripts" / "x.sh"
    ).read_text() == "echo hi"


def test_route_skills_script_never_executed(write_config, tmp_path, projects_root) -> None:
    """RCE-NEGATIVE at the ROUTE: a script that would touch a marker must not."""
    marker = tmp_path / "ROUTE_SCRIPT_PWNED"
    pwn = f"touch {marker}"
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD, "scripts/x.sh": pwn},
                "confirm_scripts": sk.SCRIPT_CONFIRM_TOKEN,
            },
        )
        assert resp.status_code == 200
    assert not marker.exists()


def test_route_skills_delete_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        resp = c.post(
            _DELETE_URL,
            json={"scope": "project", "project": "alpha", "confirm": "alpha", "name": "greeter"},
        )
        assert resp.status_code == 200
        assert resp.json()["existed"] is True
        listing = c.get(f"{_SKILLS_URL}?project=alpha")
        assert listing.json()["skills"] == []


def test_route_skills_delete_confirm_mismatch_is_400(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            _DELETE_URL,
            json={"scope": "project", "project": "alpha", "confirm": "WRONG", "name": "greeter"},
        )
        assert resp.status_code == 400


def test_route_skills_user_scope_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        wr = c.put(
            _SKILLS_URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert wr.status_code == 200
        listing = c.get(f"{_SKILLS_URL}?scope=user")
        assert listing.json()["skills"][0]["name"] == "greeter"
        deleted = c.post(
            _DELETE_URL, json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "name": "greeter"}
        )
        assert deleted.status_code == 200
    isolated = Path(os.environ["HOME"]) / ".claude" / "skills" / "greeter"
    assert not isolated.exists()


def test_route_skills_user_scope_404_when_user_off(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        assert c.get(f"{_SKILLS_URL}?scope=user").status_code == 404


def test_route_skills_user_scope_404_when_runner_missing(write_config, tmp_path) -> None:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    app = create_app(load_config(cfg))
    with TestClient(app) as c:
        app.state.runner = None
        assert c.get(f"{_SKILLS_URL}?scope=user").status_code == 404
        assert (
            c.put(
                _SKILLS_URL,
                json={
                    "scope": "user",
                    "confirm": cw.USER_SCOPE_TOKEN,
                    "name": "greeter",
                    "files": {"SKILL.md": _VALID_MD},
                },
            ).status_code
            == 404
        )


def test_route_skills_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        assert resp.status_code == 404


# --- overrides routes (three scopes) ----------------------------------------------------


def test_route_overrides_404_when_disabled(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, "") as c:
        assert c.get(f"{_OVERRIDES_URL}?project=alpha").status_code == 404
        assert (
            c.put(
                _OVERRIDES_URL,
                json={"scope": "project", "project": "alpha", "confirm": "alpha", "overrides": {}},
            ).status_code
            == 404
        )


def test_route_overrides_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_OVERRIDES_URL}?scope=bogus").status_code == 422
        assert c.put(_OVERRIDES_URL, json={"scope": "bogus", "overrides": {}}).status_code == 422


def test_route_overrides_project_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_OVERRIDES_URL}?project=alpha")
        assert read0.status_code == 200
        assert read0.json()["overrides"] == {}
        h0 = read0.json()["hash"]
        wr = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "overrides": {"greeter": "off"},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_OVERRIDES_URL}?project=alpha")
        assert read1.json()["overrides"] == {"greeter": "off"}


def test_route_overrides_local_scope_round_trip(write_config, tmp_path, projects_root) -> None:
    with _client(write_config, tmp_path, _PROJECT_ONLY) as c:
        wr = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "overrides": {"greeter": "name-only"},
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_OVERRIDES_URL}?scope=local&project=alpha")
        assert read1.json()["overrides"] == {"greeter": "name-only"}
    gitignore = (projects_root / "alpha" / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/settings.local.json" in gitignore


def test_route_overrides_local_confirm_rejects_plain_project_name(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha",  # project-scope token, not local
                "overrides": {"greeter": "off"},
            },
        )
        assert resp.status_code == 400


def test_route_overrides_user_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        read0 = c.get(f"{_OVERRIDES_URL}?scope=user")
        h0 = read0.json()["hash"]
        wr = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "overrides": {"greeter": "on"},
                "hash": h0,
            },
        )
        assert wr.status_code == 200
        read1 = c.get(f"{_OVERRIDES_URL}?scope=user")
        assert read1.json()["overrides"] == {"greeter": "on"}
    isolated = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    out = json.loads(isolated.read_text(encoding="utf-8"))
    assert out["skillOverrides"] == {"greeter": "on"}


def test_route_overrides_unknown_value_saves_with_a_warning(
    write_config, tmp_path, projects_root
) -> None:
    """TASK-524: a visibility value outside the known four saves, and the response warns."""
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "overrides": {"greeter": "bogus", "other": "off"},
            },
        )
        assert resp.status_code == 200, resp.text
        [warning] = resp.json()["warnings"]
    assert "skillOverrides['greeter'] 'bogus' is not one of the known values" in warning
    stored = json.loads((projects_root / "alpha" / ".claude" / "settings.json").read_text())
    assert stored["skillOverrides"] == {"greeter": "bogus", "other": "off"}


def test_route_overrides_missing_payload_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL, json={"scope": "project", "project": "alpha", "confirm": "alpha"}
        )
        assert resp.status_code == 422


def test_route_overrides_stale_hash_is_409(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"skillOverrides": {}}')
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "overrides": {"greeter": "off"},
                "hash": cw.hash_bytes(b"something else"),
            },
        )
        assert resp.status_code == 409


def test_route_overrides_confirm_mismatch_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "WRONG",
                "overrides": {"greeter": "off"},
            },
        )
        assert resp.status_code == 400


def test_route_overrides_write_missing_project_dir_is_404(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "noexist",
                "confirm": "noexist",
                "overrides": {"greeter": "off"},
            },
        )
        assert resp.status_code == 404


# --- the payload the Skills tab's visibility panel sends (TASK-249) ----------------------
#
# The panel PUTs the WHOLE map it shows: every row that has a value, and no row set to
# "No override". So a removed row is a key missing from the payload, and the last
# removal sends {}. These pin that the route replaces the map (never merges), keeps the
# other settings keys, and answers a refused key with the message the panel shows.


def _overrides_put(c: TestClient, overrides: dict, file_hash: str | None) -> Response:
    return c.put(
        _OVERRIDES_URL,
        json={
            "scope": "project",
            "project": "alpha",
            "confirm": "alpha",
            "overrides": overrides,
            "hash": file_hash,
        },
    )


def test_route_overrides_full_map_replace_drops_a_removed_key(
    write_config, tmp_path, projects_root
) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"model": "opus", "skillOverrides": {"gone": "off", "kept": "name-only"}})
    )
    with _client(write_config, tmp_path, _ON) as c:
        h0 = c.get(f"{_OVERRIDES_URL}?project=alpha").json()["hash"]
        assert _overrides_put(c, {"kept": "off"}, h0).status_code == 200
        assert c.get(f"{_OVERRIDES_URL}?project=alpha").json()["overrides"] == {"kept": "off"}
    on_disk = json.loads(settings.read_text(encoding="utf-8"))
    assert on_disk["model"] == "opus"  # a sibling settings key survives the replace


def test_route_overrides_empty_map_clears_every_override(
    write_config, tmp_path, projects_root
) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"skillOverrides": {"a": "off"}}))
    with _client(write_config, tmp_path, _ON) as c:
        h0 = c.get(f"{_OVERRIDES_URL}?project=alpha").json()["hash"]
        assert _overrides_put(c, {}, h0).status_code == 200
        assert c.get(f"{_OVERRIDES_URL}?project=alpha").json()["overrides"] == {}


def test_route_overrides_accepts_a_name_the_cli_shows(
    write_config, tmp_path, projects_root
) -> None:
    # A frontmatter name, a bundled skill and a plugin skill: none is a folder name.
    names = {"My Deploy Skill": "off", "doctor": "name-only", "anthropic-skills:pdf": "on"}
    with _client(write_config, tmp_path, _ON) as c:
        h0 = c.get(f"{_OVERRIDES_URL}?project=alpha").json()["hash"]
        assert _overrides_put(c, names, h0).status_code == 200
        assert c.get(f"{_OVERRIDES_URL}?project=alpha").json()["overrides"] == names


@pytest.mark.parametrize("bad", ["", "__proto__", " lead", "two  spaces", "a<b"])
def test_route_overrides_refused_key_names_it_and_writes_nothing(
    write_config, tmp_path, projects_root, bad
) -> None:
    # json.dumps keeps "__proto__" as an own key, exactly as the panel's
    # Object.fromEntries payload does, so the backend rule is what refuses it.
    with _client(write_config, tmp_path, _ON) as c:
        h0 = c.get(f"{_OVERRIDES_URL}?project=alpha").json()["hash"]
        resp = _overrides_put(c, {"ok-skill": "off", bad: "off"}, h0)
        assert resp.status_code == 422
        assert resp.json()["detail"] == f"skillOverrides has an invalid skill name: {bad!r}"
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


@pytest.mark.parametrize("bad", [[], {}, ["off"], None, 1, True])
def test_route_overrides_non_string_value_names_the_key(
    write_config, tmp_path, projects_root, bad
) -> None:
    # The panel sends a stored value it cannot show back unchanged, so the backend must
    # refuse a hand-edited list or object with a message that names the key, not a bare
    # "unhashable type" from the membership test.
    with _client(write_config, tmp_path, _ON) as c:
        h0 = c.get(f"{_OVERRIDES_URL}?project=alpha").json()["hash"]
        resp = _overrides_put(c, {"odd": bad}, h0)
        assert resp.status_code == 422
        assert resp.json()["detail"].startswith("skillOverrides['odd'] must be a string")
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


def test_route_overrides_read_returns_an_unlisted_and_an_unknown_value_as_stored(
    write_config, tmp_path, projects_root
) -> None:
    # The panel shows every stored key, also one that matches no skill folder and one
    # whose value Claude Code does not know, so the read must hand both back unchanged.
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"skillOverrides": {"orphan": "off", "odd": "maybe"}}))
    with _client(write_config, tmp_path, _ON) as c:
        read = c.get(f"{_OVERRIDES_URL}?project=alpha").json()
    assert read["overrides"] == {"orphan": "off", "odd": "maybe"}


# --- plugin-owned content rejected end to end at the route -------------------------------


def test_route_skills_rejects_plugin_marker_in_skill_md(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": "---\ndescription: x\n---\n${CLAUDE_PLUGIN_ROOT}/run.sh\n"},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "skills").exists()


def test_route_skills_rejects_plugin_marker_in_script(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {
                    "SKILL.md": _VALID_MD,
                    "scripts/x.sh": "${CLAUDE_PLUGIN_ROOT}/run.sh",
                },
                "confirm_scripts": sk.SCRIPT_CONFIRM_TOKEN,
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "skills").exists()


# --- targeted gap-closing: file/write/delete/overrides route branches --------------------


def test_route_skills_file_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_FILE_URL}?scope=bogus&name=x").status_code == 422


def test_route_skills_file_missing_name_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_FILE_URL}?scope=project&project=alpha").status_code == 422


def test_route_skills_file_user_scope_round_trip(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        c.put(
            _SKILLS_URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
            },
        )
        resp = c.get(f"{_FILE_URL}?scope=user&name=greeter")
        assert resp.status_code == 200
        assert resp.json()["content"] == _VALID_MD


def test_route_skills_file_project_scope_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_FILE_URL}?scope=project&project=alpha&name=../escape")
        assert resp.status_code == 400


def test_route_skills_file_user_scope_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_FILE_URL}?scope=user&name=../escape")
        assert resp.status_code == 400


def test_route_skills_write_non_string_hash_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": _VALID_MD},
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_skills_write_user_scope_bad_shape_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "name": "greeter",
                "files": {"SKILL.md": "---\nnever closed\n"},
            },
        )
        assert resp.status_code == 422


def test_route_skills_delete_invalid_scope_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(_DELETE_URL, json={"scope": "bogus", "name": "x", "confirm": "x"})
        assert resp.status_code == 422


def test_route_skills_delete_missing_name_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            _DELETE_URL, json={"scope": "project", "project": "alpha", "confirm": "alpha"}
        )
        assert resp.status_code == 422


def test_route_skills_delete_user_scope_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            _DELETE_URL,
            json={"scope": "user", "confirm": cw.USER_SCOPE_TOKEN, "name": "../escape"},
        )
        assert resp.status_code == 400


def test_route_skills_delete_project_scope_path_escape_is_400(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.post(
            _DELETE_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "../escape",
            },
        )
        assert resp.status_code == 400


def test_route_overrides_user_scope_corrupt_file_is_422(write_config, tmp_path) -> None:
    user_settings = Path(os.environ["HOME"]) / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True, exist_ok=True)
    user_settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_OVERRIDES_URL}?scope=user").status_code == 422


def test_route_overrides_local_scope_corrupt_file_is_422(
    write_config, tmp_path, projects_root
) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_OVERRIDES_URL}?scope=local&project=alpha").status_code == 422


def test_route_overrides_project_scope_corrupt_file_is_422(
    write_config, tmp_path, projects_root
) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    with _client(write_config, tmp_path, _ON) as c:
        assert c.get(f"{_OVERRIDES_URL}?scope=project&project=alpha").status_code == 422


def test_route_overrides_write_non_string_hash_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "overrides": {"greeter": "off"},
                "hash": 123,
            },
        )
        assert resp.status_code == 422


def test_route_overrides_write_user_scope_bad_value_is_422(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "user",
                "confirm": cw.USER_SCOPE_TOKEN,
                "overrides": {"greeter": ["on"]},
            },
        )
        assert resp.status_code == 422


def test_route_overrides_write_local_scope_bad_value_is_422(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "local",
                "project": "alpha",
                "confirm": "alpha (local)",
                "overrides": {"greeter": 5},
            },
        )
        assert resp.status_code == 422


def test_list_skills_survives_an_unreadable_skill_md(tmp_path, monkeypatch):
    # Stated intent is "a single bad skill must never break the whole listing", but only
    # parse errors were caught: an OSError from read_bytes propagated and failed the WHOLE
    # listing, so one bad skill hid every good one. Matches supervisor.list_background_jobs,
    # which catches OSError per entry for exactly this reason.
    root = tmp_path / "skills"
    (root / "good").mkdir(parents=True)
    (root / "good" / "SKILL.md").write_text("---\nname: good\ndescription: fine\n---\nbody\n")
    (root / "bad").mkdir(parents=True)
    bad_md = root / "bad" / "SKILL.md"
    bad_md.write_text("---\nname: bad\ndescription: x\n---\nbody\n")

    real_open = sk.os.open

    def _boom(path, *args, **kwargs):
        if Path(path) == bad_md:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sk.os, "open", _boom)

    listed = {s["name"]: s for s in sk.list_skills(tmp_path)}

    assert set(listed) == {"good", "bad"}  # the whole listing survived
    assert listed["good"]["description"] == "fine"  # the good skill is intact
    assert "could not be read" in listed["bad"]["frontmatter_error"]  # reported, not silent


def test_list_skills_reports_an_unlistable_member_tree(tmp_path, monkeypatch):
    # The other OSError source the docstring names: enumerating members via resolve()/
    # rglob(). It used to fail the whole listing too. Reported via `files_error` rather
    # than a bare empty `files`, which would be indistinguishable from a skill that
    # genuinely has no members — an unreadable directory would look healthy.
    root = tmp_path / "skills"
    (root / "s").mkdir(parents=True)
    (root / "s" / "SKILL.md").write_text("---\nname: s\ndescription: fine\n---\nbody\n")

    def _boom(self, pattern):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "rglob", _boom)

    listed = sk.list_skills(tmp_path)

    assert len(listed) == 1  # the listing survived
    assert listed[0]["description"] == "fine"  # frontmatter still read
    assert listed[0]["files"] == []
    assert "could not be listed" in listed[0]["files_error"]  # reported, not silent


def test_list_skills_error_text_survives_an_errno_less_oserror(tmp_path, monkeypatch):
    # `OSError.strerror` is only populated when the exception carries an errno, so a bare
    # OSError rendered "could not be read: None" — telling a dashboard user no more than the
    # silently-empty listing this arm replaces. The point of reporting is that it reports.
    root = tmp_path / "skills"
    (root / "s").mkdir(parents=True)
    (root / "s" / "SKILL.md").write_text("---\nname: s\ndescription: fine\n---\nbody\n")

    def _boom(path, *args, **kwargs):
        raise OSError("device fell off the bus")

    monkeypatch.setattr(sk.os, "open", _boom)

    listed = sk.list_skills(tmp_path)

    assert "None" not in listed[0]["frontmatter_error"]
    assert "device fell off the bus" in listed[0]["frontmatter_error"]


# --- TASK-449.6: every documented field, every CLI-parsed value -------------------------
#
# Sources: https://code.claude.com/docs/en/skills#frontmatter-reference (DOC) and the
# skill loader of the installed CLI 2.1.318 (CLI: `oPe`, `bye`, `tv`/`fu`, `wke`, `dq`,
# `tdo`, `Ka`, `jMt`, `ace`). Each case is one SKILL.md header line.


def _fm(*lines: str) -> str:
    return "---\n" + "\n".join(lines) + "\n---\nBody.\n"


_DOCUMENTED_FIELDS = [
    pytest.param("name: my-skill", id="DOC-name"),
    pytest.param("description: What this skill does", id="DOC-description"),
    pytest.param("when_to_use: When the user asks for a deploy", id="DOC-when_to_use"),
    pytest.param("argument-hint: '[issue-number]'", id="DOC-argument-hint"),
    pytest.param("arguments: issue branch", id="DOC-arguments-string"),
    pytest.param("arguments: [issue, branch]", id="DOC-arguments-list"),
    pytest.param("disable-model-invocation: true", id="DOC-disable-model-invocation"),
    pytest.param("user-invocable: false", id="DOC-user-invocable"),
    pytest.param("allowed-tools: Read Grep", id="DOC-allowed-tools-space"),
    pytest.param("allowed-tools: Read, Grep", id="DOC-allowed-tools-comma"),
    pytest.param("allowed-tools: [Read, 'Bash(git add *)']", id="DOC-allowed-tools-list"),
    pytest.param("disallowed-tools: AskUserQuestion", id="DOC-disallowed-tools-string"),
    pytest.param("disallowed-tools: [AskUserQuestion, Bash]", id="DOC-disallowed-tools-list"),
    pytest.param("model: sonnet", id="DOC-model-alias"),
    pytest.param("model: claude-opus-5-5", id="DOC-model-id"),
    pytest.param("model: inherit", id="DOC-model-inherit"),
    *[
        pytest.param(f"effort: {level}", id=f"DOC-effort-{level}")
        for level in ("low", "medium", "high", "xhigh", "max")
    ],
    pytest.param("context: fork", id="DOC-context"),
    pytest.param("agent: Explore", id="DOC-agent"),
    pytest.param("background: false", id="DOC-background"),
    pytest.param(
        "hooks:\n  PreToolUse:\n    - matcher: Bash\n      hooks:\n"
        "        - {type: command, command: ./check.sh, once: true}",
        id="DOC-hooks",
    ),
    pytest.param("paths: 'src/**/*.ts, tests/**'", id="DOC-paths-string"),
    pytest.param("paths: [src/**/*.ts, tests/**]", id="DOC-paths-list"),
    pytest.param("shell: bash", id="DOC-shell-bash"),
    pytest.param("shell: powershell", id="DOC-shell-powershell"),
    pytest.param("metadata: {owner: platform, tier: 2}", id="DOC-metadata"),
    pytest.param("license: Apache-2.0", id="DOC-license"),
    pytest.param("compatibility: Claude Code with git on PATH", id="DOC-compatibility"),
]


@pytest.mark.parametrize("line", _DOCUMENTED_FIELDS)
def test_every_documented_field_saves_with_its_documented_type(line: str) -> None:
    sk.validate_skill_md_content(_fm(line))  # no raise


_CLI_PARSED = [
    *[
        pytest.param(f"{key}: {word}", id=f"CLI-{key}-{word}")
        for key in ("disable-model-invocation", "user-invocable", "background")
        for word in ("yes", "No", "ON", "off", "1", "0", "'TRUE'", "' yes '")
    ],
    pytest.param("user-invocable:", id="CLI-user-invocable-null-reads-false"),
    pytest.param("fallback: true", id="CLI-fallback"),
    pytest.param("version: 1.2.0", id="CLI-version"),
    pytest.param("version: 2", id="CLI-version-number"),
    pytest.param("disallowedTools: Bash", id="CLI-disallowedTools-camel"),
    pytest.param("created_by: dream-proposal", id="CLI-created_by"),
    pytest.param("improved_by: dream-proposal", id="CLI-improved_by"),
    pytest.param("description: 42", id="CLI-description-number"),
    pytest.param("description: true", id="CLI-description-bool"),
    pytest.param("name: My Deploy Skill", id="CLI-name-with-spaces"),
    pytest.param("name: anthropic-skills:pdf", id="CLI-name-with-colon"),
    pytest.param("name: déployer", id="CLI-name-non-ascii"),
    pytest.param("name: 7", id="CLI-name-number"),
    pytest.param("argument-hint: [issue-number]", id="CLI-argument-hint-yaml-list"),
    pytest.param("argument-hint: 3", id="CLI-argument-hint-number"),
    pytest.param("when_to_use: [deploy, ship]", id="CLI-when_to_use-list"),
    pytest.param("allowed-tools: ''", id="CLI-allowed-tools-empty"),
    pytest.param("allowed-tools: '*'", id="CLI-allowed-tools-star"),
    pytest.param("allowed-tools:", id="CLI-allowed-tools-null"),
    pytest.param("arguments: ''", id="CLI-arguments-empty"),
    pytest.param("effort: HIGH", id="CLI-effort-upper"),
    pytest.param("effort: med", id="CLI-effort-alias"),
    pytest.param("effort: 3", id="CLI-effort-integer"),
    pytest.param("effort: 3 tokens", id="CLI-effort-parseInt"),
    pytest.param("model: ' opus '", id="CLI-model-padded"),
    pytest.param("model: INHERIT", id="CLI-model-inherit-upper"),
    pytest.param("shell: ' PowerShell '", id="CLI-shell-case"),
    pytest.param("paths: [[src/**, docs/**], tests/**]", id="CLI-paths-nested-list"),
    pytest.param("paths: '{src,lib}/**/*.py'", id="CLI-paths-brace"),
    pytest.param("hooks:", id="CLI-hooks-null"),
    pytest.param("PreToolUse: []", id="CLI-top-level-event-empty"),
    pytest.param("metadata:", id="CLI-metadata-null"),
]


@pytest.mark.parametrize("line", _CLI_PARSED)
def test_every_cli_parsed_value_saves(line: str) -> None:
    sk.validate_skill_md_content(_fm(line))  # no raise


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("Just the instructions.\n", id="no-frontmatter"),
        pytest.param("\ufeff---\ndescription: d\n---\nbody\n", id="bom-then-frontmatter"),
        pytest.param("---\n---\nbody\n", id="empty-frontmatter"),
        pytest.param("---\n\n---\nbody\n", id="blank-frontmatter"),
        pytest.param("\n---\ndescription: d\n---\n", id="fence-not-on-line-1-is-content"),
    ],
)
def test_skill_md_without_frontmatter_fields_saves(content: str) -> None:
    # The docs: "All fields are optional" and the frontmatter is read only when the opening
    # `---` is the file's first line; otherwise the whole file is the skill's content. The
    # CLI drops a leading BOM first (its `qw`).
    sk.validate_skill_md_content(content)  # no raise


# Refused, and whether a load may remove the field (the CLI treats it as absent: True), or
# not (the CLI does something else with it: False).
_REFUSED = [
    pytest.param("description: ''", True, id="description-blank"),
    pytest.param("description: [a]", True, id="description-list"),
    pytest.param("name: ''", True, id="name-empty"),
    pytest.param("name: " + "x" * 257, True, id="name-over-256"),
    pytest.param("name: '  lead'", False, id="name-leading-space"),
    pytest.param("name: 'a  b'", False, id="name-double-space"),
    pytest.param("name: 'a<b'", False, id="name-angle-bracket"),
    pytest.param('name: "a\\u200bb"', False, id="name-zero-width"),
    pytest.param('name: "a\\u00a0b"', False, id="name-no-break-space"),
    pytest.param('name: "\\u202ertl"', False, id="name-bidi"),
    pytest.param("name: {a: 1}", False, id="name-mapping"),
    pytest.param("disable-model-invocation: maybe", True, id="dmi-unreadable"),
    pytest.param("background: maybe", True, id="background-unreadable"),
    pytest.param("fallback: 2", True, id="fallback-unreadable"),
    pytest.param("user-invocable: maybe", False, id="user-invocable-unreadable-reads-false"),
    pytest.param("allowed-tools: 5", True, id="allowed-tools-number"),
    pytest.param("allowed-tools: {Read: 1}", True, id="allowed-tools-mapping"),
    pytest.param("allowed-tools: [Read, 5]", False, id="allowed-tools-non-string-entry"),
    pytest.param("disallowed-tools: true", True, id="disallowed-tools-bool"),
    pytest.param("arguments: 5", True, id="arguments-number"),
    pytest.param("arguments: issue 2", False, id="arguments-digit-name"),
    pytest.param("arguments: [issue, '']", False, id="arguments-blank-entry"),
    pytest.param("arguments: [issue, 5]", False, id="arguments-non-string-entry"),
    pytest.param("effort: true", True, id="effort-bool"),
    pytest.param("effort: [banana]", True, id="effort-list-of-a-word"),
    pytest.param("context: 5", True, id="context-number"),
    pytest.param("shell: 5", True, id="shell-number"),
    pytest.param("model: ''", True, id="model-blank"),
    pytest.param("model: 5", True, id="model-number"),
    pytest.param("shell: ''", True, id="shell-blank"),
    pytest.param("metadata: [1, 2]", True, id="metadata-list"),
    pytest.param("metadata: text", True, id="metadata-string"),
    pytest.param("paths: 5", True, id="paths-number"),
    pytest.param("paths: [src/**, 5]", False, id="paths-non-string-entry"),
    pytest.param("hooks: [x]", False, id="hooks-list-drops-allowed-tools"),
    pytest.param("hooks: on-commit", False, id="hooks-string"),
    pytest.param("hooks: false", True, id="hooks-false"),
    pytest.param("argument-hint: {a: 1}", False, id="argument-hint-mapping"),
    pytest.param("agent: [{a: 1}]", False, id="agent-list-of-mapping"),
    pytest.param("created_by: alice", True, id="created_by-other"),
    pytest.param("PreToolUse: [{hooks: []}]", False, id="top-level-PreToolUse"),
    pytest.param("PermissionRequest: {a: 1}", False, id="top-level-PermissionRequest"),
    pytest.param("disallowed-tools: Bash\ndisallowedTools: Read", True, id="both-denylists"),
]


@pytest.mark.parametrize(("line", "droppable"), _REFUSED)
def test_a_value_the_cli_rejects_or_ignores_is_refused(line: str, droppable: bool) -> None:
    with pytest.raises(cw.InvalidCandidateError) as excinfo:
        sk.validate_skill_md_content(_fm(line))
    assert isinstance(excinfo.value, sk._IgnoredFieldError) is droppable


@pytest.mark.parametrize(
    ("line", "warned"),
    [
        pytest.param("effort: banana", "'effort' 'banana'", id="effort-unknown"),
        pytest.param("effort: ' high'", "'effort' ' high'", id="effort-not-trimmed"),
        pytest.param("context: forked", "'context' 'forked'", id="context-other"),
        pytest.param("context: Fork", "'context' 'Fork'", id="context-case"),
        pytest.param("shell: zsh", "'shell' 'zsh'", id="shell-unknown"),
    ],
)
def test_a_word_outside_a_known_list_saves_with_a_warning(line: str, warned: str) -> None:
    """TASK-524: effort, context and shell offer known values; another word warns."""
    with cw.collect_value_warnings() as warnings:
        sk.validate_skill_md_content(_fm(line))
    assert warnings and all(warned in w for w in warnings), warnings
    assert "is not one of the known values" in warnings[0]
    # The load conversion leaves such a field in place: it is not one Claude Code ignores.
    assert sk._ignorable_keys(_fm(line)) is None


@pytest.mark.parametrize(
    ("line", "field"),
    [
        # A string to the CLI's YAML 1.2, a boolean to PyYAML: the text differs.
        ("description: yes", "description"),
        # 8 to PyYAML, 10 to YAML 1.2.
        ("effort: 010", "effort"),
        # A string to PyYAML (it needs a dot), the number 1 to YAML 1.2, so `true` to the CLI.
        ("disable-model-invocation: 1e0", "disable-model-invocation"),
        # A date to PyYAML, a string to YAML 1.2.
        ("version: 2024-01-01", "version"),
        # A string to PyYAML, the octal number 7 to YAML 1.2, so the name "7" to the CLI.
        ("name: 0o7", "name"),
    ],
)
def test_a_value_yaml_1_1_and_1_2_read_differently_is_refused_by_name(
    line: str, field: str
) -> None:
    with pytest.raises(cw.InvalidCandidateError, match=f"'{field}' reads differently"):
        sk.validate_skill_md_content(_fm(line))


def test_a_yaml_1_1_boolean_that_the_cli_also_reads_as_that_boolean_saves() -> None:
    # `yes` is a string to YAML 1.2, but the CLI's boolean reader takes the string `yes` as
    # true, so PyYAML's True and the CLI agree: the docs list yes/no/on/off for booleans.
    readings = sk.validate_frontmatter({"user-invocable": True})
    sk.validate_skill_md_content(_fm("user-invocable: yes"))  # no raise
    assert readings == {"user-invocable": ("bool-or-null", True)}


@pytest.mark.parametrize(
    "hook_line",
    ["async: yes", "timeout: 1_000", "timeout: 010", "once: on", "timeout: 0b101"],
)
def test_a_yaml_1_1_only_spelling_inside_frontmatter_hooks_is_refused(hook_line: str) -> None:
    # Coordinator scope addition, 2026-11-05: the shared seam walks nested values, so a
    # skill's frontmatter hook with `async: yes` or `timeout: 1_000` is refused. The CLI's
    # YAML 1.2 reads both as strings and rejects or ignores them.
    content = _fm(
        "hooks:",
        "  PreToolUse:",
        "    - hooks:",
        f"        - {{type: command, command: ./x.sh, {hook_line}}}",
    )
    with pytest.raises(cw.InvalidCandidateError, match="nested value"):
        sk.validate_skill_md_content(content)


def test_a_plain_spelling_inside_frontmatter_hooks_saves() -> None:
    content = _fm(
        "hooks:",
        "  PreToolUse:",
        "    - hooks:",
        "        - {type: command, command: ./x.sh, async: true, timeout: 1000}",
    )
    sk.validate_skill_md_content(content)  # no raise


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("x" * 256, True),
        ("x" * 257, False),
        ("\U0001f600" * 128, True),  # 256 UTF-16 units: JavaScript counts a surrogate pair as 2
        ("\U0001f600" * 129, False),
        ("doctor", True),
        ("apps/web:deploy", True),
        ("", False),
        ("tab\there", False),
        ("a\u3164b", False),  # Hangul filler: a letter to Python, a blank to the CLI
        ("a\u2800b", False),  # Braille blank
        ("caf\u0065\u0301", False),  # a combining mark: narrower than the CLI, on purpose
        ("a&lt;b", False),  # the CLI turns &lt; back into < before it checks
    ],
)
def test_cli_shows_name(name: str, shown: bool) -> None:
    assert sk.cli_shows_name(name) is shown


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (None, "null"),
        (5, "5"),
        (5.0, "5"),
        (-0.0, "0"),
        (0.5, "0.5"),
        (1e21, "1e+21"),
        (10**21, "1e+21"),
        (1.5e-7, "1.5e-7"),
        (0.000001, "0.000001"),
        (123456.789, "123456.789"),
        (float("inf"), "Infinity"),
        (float("nan"), "NaN"),
        (10**400, "Infinity"),
        (["a", None, 1, ["b"]], "a,,1,b"),
        ({"a": 1}, "[object Object]"),
    ],
)
def test_js_string_matches_javascript(value: object, text: str) -> None:
    assert sk._js_string(value) == text


def test_js_string_refuses_a_value_javascript_never_sees() -> None:
    import datetime

    with pytest.raises(TypeError):
        sk._js_string(datetime.date(2024, 1, 1))


def test_effort_reader_follows_the_cli() -> None:
    assert sk._cli_effort(4.0) == 4
    assert sk._cli_effort(4.5) == 4  # String(4.5) is "4.5", and parseInt reads 4
    # dq stringifies anything that is not a number: a list joins, a mapping never matches.
    assert sk._cli_effort([1]) == 1
    assert sk._cli_effort(["High"]) == "high"
    assert sk._cli_effort({"a": 1}) is None


def test_list_skills_shows_what_the_cli_reads(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for name, text in {
        "bare": "Only instructions.\n",
        "yes-flag": "---\ndescription: '  padded  '\ndisable-model-invocation: 'Yes'\n---\nb\n",
    }.items():
        (root / name).mkdir(parents=True)
        (root / name / sk.SKILL_FILENAME).write_bytes(text.encode("utf-8"))
    listing = {item["name"]: item for item in sk.list_skills(tmp_path)}
    assert "frontmatter_error" not in listing["bare"]
    assert listing["bare"]["description"] is None
    assert listing["bare"]["disable_model_invocation"] is False
    # The string 'Yes' was `True` under the old bool(...) check only by accident; it is
    # true now because the CLI's reader says so. The description is trimmed, as the CLI shows it.
    assert listing["yes-flag"]["disable_model_invocation"] is True
    assert listing["yes-flag"]["description"] == "padded"


def test_list_skills_reports_a_yaml_version_mismatch(tmp_path: Path) -> None:
    (tmp_path / "skills" / "s").mkdir(parents=True)
    (tmp_path / "skills" / "s" / sk.SKILL_FILENAME).write_bytes(b"---\neffort: 010\n---\nb\n")
    (item,) = sk.list_skills(tmp_path)
    assert "'effort' reads differently" in item["frontmatter_error"]


@pytest.mark.parametrize("unclosed", ["---\nname: x\n", "---", "--- \t\r\ndescription: d"])
def test_an_unclosed_frontmatter_block_is_refused(unclosed: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="never closes it"):
        sk.validate_skill_md_content(unclosed)


# --- TASK-449.6: convert on load ------------------------------------------------------


def _skill(tmp_path: Path, text: str, name: str = "s") -> Path:
    path = tmp_path / "skills" / name / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def test_load_removes_fields_the_cli_ignores_and_says_so(tmp_path: Path) -> None:
    raw = (
        "---\ndescription: d\nshell: 5  # my shell\nbackground: maybe\n"
        "metadata:\n  - not\n  - a map\nname: kept\n---\nBody.\n"
    )
    path = _skill(tmp_path, raw)
    content, file_hash, exists, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert exists
    assert content == "---\ndescription: d\nname: kept\n---\nBody.\n"
    assert file_hash == cw.hash_bytes(path.read_bytes())  # the file ON DISK, for the 409 guard
    assert note is not None
    for key in ("'shell'", "'background'", "'metadata'"):
        assert key in note
    sk.validate_skill_md_content(content)  # the converted text saves
    # The plain read never converts: it is the file as it is on disk.
    assert sk.read_skill_file(tmp_path, "s")[0] == raw


def test_load_keeps_a_bom_and_crlf_line_ends_when_it_converts(tmp_path: Path) -> None:
    _skill(tmp_path, "\ufeff---\r\ndescription: d\r\ncontext: 5\r\n---\r\nBody.\r\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == "\ufeff---\r\ndescription: d\r\n---\r\nBody.\r\n"
    assert note is not None


def test_load_converts_the_last_field_without_a_blank_line(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\nshell: 5\n---\nBody.\n")
    content, _, _, _ = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == "---\ndescription: d\n---\nBody.\n"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("---\ndescription: d\n---\nBody.\n", id="nothing-to-convert"),
        pytest.param("---\nhooks: [x]\n---\nBody.\n", id="hooks-not-droppable"),
        pytest.param("---\nuser-invocable: maybe\n---\nb\n", id="user-invocable-not-droppable"),
        pytest.param("---\n{shell: 5}\n---\nb\n", id="flow-style-root"),
        pytest.param("---\nshell: 5\nshell: bash\n---\nb\n", id="duplicate-key"),
        pytest.param("---\nbackground: x\nbackground: y\n---\nb\n", id="duplicate-ignored-key"),
        pytest.param("---\n  description: d\n  shell: 5\n---\nb\n", id="indented-root"),
        pytest.param("---\nx: &x {shell: 5}\n<<: *x\n---\nb\n", id="field-from-a-merge"),
        pytest.param("---\nshell: 5\neffort: 010\n---\nb\n", id="also-a-mismatch"),
        pytest.param("---\nnever closed\n", id="unparseable"),
    ],
)
def test_load_never_converts_what_it_cannot_prove_equivalent(tmp_path: Path, raw: str) -> None:
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None
    assert content == raw


def test_load_redacts_after_converting(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\napi_key: sk-not-a-real-key-0000\nshell: 5\n---\nb\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is not None
    assert "sk-not-a-real-key-0000" not in content
    assert "shell" not in content


def test_load_does_not_convert_a_supporting_file(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\n---\nb\n")
    (tmp_path / "skills" / "s" / "notes.md").write_bytes(b"---\nshell: 5\n---\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s", "notes.md")
    assert note is None and "shell: 5" in content


def test_route_returns_the_converted_text_and_saves_it(
    write_config, tmp_path, projects_root
) -> None:
    raw = "---\ndescription: d\nbackground: maybe\n---\nBody.\n"
    path = projects_root / "alpha" / ".claude" / "skills" / "greeter" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    # Byte-exact LF: text mode writes CRLF on Windows, and the route returns the file's
    # own line endings (Windows CI, PR 1810).
    path.write_bytes(raw.encode("utf-8"))
    with _client(write_config, tmp_path, _ON) as c:
        view = c.get(f"{_FILE_URL}?project=alpha&name=greeter").json()
        assert view["converted"] and "'background'" in view["converted"]
        assert view["content"] == "---\ndescription: d\n---\nBody.\n"
        body = {
            "scope": "project",
            "project": "alpha",
            "confirm": "alpha",
            "name": "greeter",
            "files": {"SKILL.md": view["content"]},
            "hash": view["hash"],
        }
        assert c.put(_SKILLS_URL, json=body).status_code == 200
        again = c.get(f"{_FILE_URL}?project=alpha&name=greeter").json()
        assert again["converted"] is None
    assert path.read_text() == "---\ndescription: d\n---\nBody.\n"


def test_route_user_scope_file_read_carries_the_converted_field(write_config, tmp_path) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        view = c.get(f"{_FILE_URL}?scope=user&name=missing").json()
        assert view["exists"] is False and view["converted"] is None


# --- TASK-449.6: skillOverrides keys and values ----------------------------------------


@pytest.mark.parametrize(
    "key", ["My Deploy Skill", "doctor", "anthropic-skills:pdf", "deploy.staging", "x" * 256]
)
def test_skill_overrides_accepts_every_name_the_cli_can_match(key: str) -> None:
    # The 2.1.318 settings schema is a record of ANY string to the four values, matched
    # exactly against skill names, and a frontmatter `name` need not be directory-shaped.
    sk.validate_skill_overrides({key: "off"})  # no raise


@pytest.mark.parametrize("key", ["", "__proto__", " lead", "a  b", "x<y", "x" * 257, "a\u200bb"])
def test_skill_overrides_refuses_a_key_no_skill_can_have(key: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="invalid skill name"):
        sk.validate_skill_overrides({key: "off"})


def test_skill_override_values_match_the_docs_and_the_cli() -> None:
    assert frozenset({"on", "name-only", "user-invocable-only", "off"}) == sk.SKILL_OVERRIDE_VALUES


@pytest.mark.parametrize(
    "key",
    [
        "description",
        "name",
        "when_to_use",
        "argument-hint",
        "arguments",
        "disable-model-invocation",
        "disallowed-tools",
        "model",
        "effort",
        "context",
        "agent",
        "background",
        "fallback",
        "paths",
        "shell",
        "version",
        "created_by",
        "improved_by",
    ],
)
def test_an_empty_field_is_not_set(key: str) -> None:
    # A YAML null (`key:` with no value) is "not set" for the CLI: every reader returns
    # its absent value for it. Only `user-invocable` differs (it reads false), tested above.
    sk.validate_skill_md_content(_fm(f"{key}:"))  # no raise


@pytest.mark.parametrize(
    ("line", "entries"),
    [
        ('allowed-tools: "Read,, Grep"', ("Read", "Grep")),
        ('allowed-tools: "\\tRead Grep, "', ("Read", "Grep")),
        (
            "allowed-tools: 'Agent(worker, researcher), Read'",
            ("Agent(worker, researcher)", "Read"),
        ),
    ],
)
def test_tool_lists_split_like_the_cli(line: str, entries: tuple[str, ...]) -> None:
    content = _fm(line)
    readings = sk._check_skill_md(content)
    assert readings["allowed-tools"] == ("list", tuple(("str", e) for e in entries))


def test_a_boolean_field_refuses_a_list() -> None:
    with pytest.raises(sk._IgnoredFieldError):
        sk.validate_skill_md_content(_fm("background: [true]"))


def test_shell_refuses_a_value_javascript_never_sees() -> None:
    import datetime

    with pytest.raises(sk._IgnoredFieldError):
        sk._read_shell(datetime.date(2024, 1, 1))


@pytest.mark.parametrize(
    "hooks",
    [
        # A date VALUE in a text field: a date to PyYAML, a string to YAML 1.2.
        "hooks: {PreToolUse: [{hooks: [{type: command, command: x, statusMessage: 2024-01-01}]}]}",
        # A date KEY inside an MCP tool's free-form input: no JavaScript key is a date.
        "hooks: {PreToolUse: [{hooks: [{type: mcp_tool, server: s, tool: t, "
        "input: {2024-01-01: y}}]}]}",
    ],
)
def test_a_date_inside_hooks_is_refused(hooks: str) -> None:
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' reads differently"):
        sk.validate_skill_md_content(_fm(hooks))


def test_a_whole_float_inside_hooks_equals_its_int() -> None:
    # JavaScript has one number type, so a timeout of 5.0 and of 5 are the same value.
    sk.validate_skill_md_content(
        _fm("hooks: {PreToolUse: [{hooks: [{type: command, command: x, timeout: 5.0}]}]}")
    )


def test_a_top_level_key_javascript_cannot_name_is_refused() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="a key Claude Code cannot read"):
        sk.validate_skill_md_content(_fm("? !!binary aGk=\n: x"))


def test_name_refuses_a_hangul_jamo_the_cli_checks_by_script() -> None:
    assert not sk.cli_shows_name("aᆨb")


def test_load_does_not_remove_a_field_another_field_aliases(tmp_path: Path) -> None:
    # Removing `shell` would leave `*s` with no anchor: the converted text would not parse,
    # so nothing is converted and the save is refused instead.
    raw = "---\nshell: &s zsh\nagent: *s\n---\nb\n"
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None and content == raw


# --- TASK-449.6 review round 1 ---------------------------------------------------------


def _deep_hooks(levels: int) -> str:
    return _fm("hooks: {PreToolUse: [{hooks: [" + "{a: " * levels + "1" + "}" * levels + "]}]}")


def _deep_input(levels: int) -> str:
    return "{a: " * levels + "1" + "}" * levels


def test_a_deep_hooks_value_is_a_422_on_every_path(tmp_path: Path) -> None:
    # PyYAML composes 300 levels inside an mcp_tool `input`; reading them back must be a
    # refusal on write, an error entry on list, and a plain load, never a 500.
    content = _fm(
        "hooks: {PreToolUse: [{hooks: [{type: mcp_tool, server: s, tool: t, input: "
        + _deep_input(300)
        + "}]}]}"
    )
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_md_content(content)
    _skill(tmp_path, content)
    (item,) = sk.list_skills(tmp_path)
    assert item["frontmatter_error"]
    assert sk.read_skill_file_for_edit(tmp_path, "s")[3] is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("---\u00a0\nallowed-tools: 5\n---\nbody\n", id="nbsp-after-fence"),
        pytest.param("---\f\nhooks: [1]\n---\nbody\n", id="form-feed-after-fence"),
        pytest.param("---\nname: 'a<b'\nx---body\n", id="closing-fence-mid-line"),
    ],
)
def test_a_fence_only_the_cli_reads_is_refused(content: str) -> None:
    # Claude Code's fence regex takes JavaScript whitespace after `---`, and ends the header
    # at the first `---` anywhere. Accepting these as "no frontmatter" would save fields that
    # were never checked.
    with pytest.raises(cw.InvalidCandidateError, match="frontmatter fence that Claude Code"):
        sk.validate_skill_md_content(content)


_GUARD_HOOK = "{PreToolUse: [{matcher: Bash, hooks: [{type: command, command: 'true'}]}]}"


@pytest.mark.parametrize(
    "line",
    [
        f"effort: {_GUARD_HOOK}",
        f"custom: {_GUARD_HOOK}",
        "custom: {matcher: Bash, hooks: [{type: command, command: x}]}",
        "custom: [[{PermissionRequest: [x]}]]",
        "PreToolUse: [{hooks: []}]",
    ],
)
def test_hooks_outside_hooks_are_refused_whole(line: str) -> None:
    # The CLI's `Hse` scan: hook definitions in any other field (three levels deep) make it
    # load the skill with no hooks and no allowed-tools.
    with pytest.raises(cw.InvalidCandidateError, match="hook definitions outside 'hooks'"):
        sk.validate_skill_md_content(_fm("allowed-tools: Bash", line))


@pytest.mark.parametrize(
    "line",
    [
        f"metadata: {_GUARD_HOOK}",  # `metadata` is one of the keys the scan skips
        "custom: [[[[{PreToolUse: [x]}]]]]",  # deeper than the scan's three levels
        "custom: {PostToolUse: [x]}",  # not a guard event, and not shaped like a matcher
        # A typed hook entry inside a `hooks` list is not scanned further.
        "Stop: {hooks: [{type: x, a: {PreToolUse: [1]}}]}",
    ],
)
def test_hook_shaped_data_the_cli_does_not_flag_still_saves(line: str) -> None:
    sk.validate_skill_md_content(_fm(line))  # no raise


@pytest.mark.parametrize(
    "hooks",
    [
        "hooks: {PreToolUse: 5}",
        "hooks: {PermissionRequest: {}}",
        "hooks: {Unknown: {PreToolUse: [x]}}",
        "hooks: {matcher: Bash, hooks: [{type: command, command: y}]}",
    ],
)
def test_a_hooks_value_that_cancels_the_skills_hooks_is_refused(hooks: str) -> None:
    # The CLI's `_Ce` guards: these drop every hook of the skill and its allowed-tools.
    with pytest.raises(cw.InvalidCandidateError, match="'hooks'"):
        sk.validate_skill_md_content(_fm(hooks))


@pytest.mark.parametrize(
    "hooks",
    [
        "hooks: {Stop: 5}",
        "hooks: {Unknown: [x]}",
        "hooks: {PostToolUse: null}",
        "hooks: {Stop: [5]}",
        "hooks: {Stop: [{hooks: x}]}",
    ],
)
def test_a_hooks_entry_the_cli_would_drop_is_refused(hooks: str) -> None:
    # TASK-482: skill hooks go through the hooks surface's validator, as subagent hooks do.
    # The CLI drops such an entry and keeps the rest; this surface refuses the save instead.
    with pytest.raises(cw.InvalidCandidateError, match="'hooks': "):
        sk.validate_skill_md_content(_fm(hooks))


def test_load_never_removes_a_collection_the_cli_may_scan(tmp_path: Path) -> None:
    raw = "---\nallowed-tools: Bash\nshell: [zsh]\n---\nb\n"
    _skill(tmp_path, raw)
    assert sk.read_skill_file_for_edit(tmp_path, "s")[3] is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('---\ndisable-model-invocation: !!bool "true"\n---\nb\n', id="tagged-bool"),
        pytest.param('---\ndescription: !!int "5"\n---\nb\n', id="tagged-description"),
        pytest.param(
            "---\ndisallowed-tools: false\ndisallowedTools: Bash\n---\nb\n", id="denylist-fallback"
        ),
    ],
)
def test_load_never_removes_a_field_the_cli_reads(tmp_path: Path, raw: str) -> None:
    # A tagged scalar may be read by the CLI (Bun reads `!!bool "true"` as the string
    # "true", which its boolean reader takes as true). An ignored `disallowed-tools` still
    # hides `disallowedTools` (`??`), so removing it would deny Bash. All three are refused
    # on save, and none is converted on load.
    with pytest.raises(cw.InvalidCandidateError) as excinfo:
        sk.validate_skill_md_content(raw)
    assert not isinstance(excinfo.value, sk._IgnoredFieldError)
    _skill(tmp_path, raw)
    assert sk.read_skill_file_for_edit(tmp_path, "s")[3] is None


def test_load_converts_every_ignored_field_in_one_validation(tmp_path: Path, monkeypatch) -> None:
    fields = ["background: maybe", "shell: 5", "context: 5", "model: ''", "created_by: me"]
    _skill(tmp_path, _fm("description: d", *fields, "metadata: [x]"))
    calls = []
    real = sk.validate_skill_md_content
    monkeypatch.setattr(sk, "validate_skill_md_content", lambda c: calls.append(1) or real(c))
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == _fm("description: d")
    assert note is not None and len(calls) == 1


def test_effort_reads_a_list_as_the_cli_does() -> None:
    sk.validate_skill_md_content(_fm("effort: [high]"))  # String([high]) is "high"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        # Checked with node: JavaScript keeps the shortest round-trip digits of a double.
        (12345678901234567890, "12345678901234567000"),
        (2**53, "9007199254740992"),
        (2**53 + 1, "9007199254740992"),
        (float(2**60), "1152921504606847000"),
        (9007199254740993.0, "9007199254740992"),
        (0.1 + 0.2, "0.30000000000000004"),
        (123e-20, "1.23e-18"),
    ],
)
def test_js_string_loses_digits_past_2_53_as_javascript_does(value: object, text: str) -> None:
    assert sk._js_string(value) == text


def test_an_empty_user_invocable_reads_false() -> None:
    assert sk.validate_frontmatter({"user-invocable": None}) == {
        "user-invocable": ("bool-or-null", False)
    }


def test_effort_reader_ignores_a_value_javascript_never_sees() -> None:
    import datetime

    assert sk._cli_effort(datetime.date(2024, 1, 1)) is None


@pytest.mark.parametrize(
    "hooks",
    [
        "hooks: {PreToolUse: [5]}",
        "hooks: {PreToolUse: [{matcher: Bash, hooks: x}]}",
        "hooks: {PermissionRequest: [{hooks: [5]}]}",
        "hooks: {Stop: [{a: {PreToolUse: [1]}}]}",
    ],
)
def test_a_matcher_group_that_cancels_the_skills_hooks_is_refused(hooks: str) -> None:
    # The CLI's `Vn`: under a guard event, one bad group or entry is not dropped alone; the
    # skill then loads with no hooks and no allowed-tools. The hooks validator refuses each.
    with pytest.raises(cw.InvalidCandidateError, match="'hooks': "):
        sk.validate_skill_md_content(_fm("allowed-tools: Bash", hooks))


def test_the_docs_argument_hint_example_saves_and_lists(tmp_path: Path) -> None:
    # The frontmatter reference's own example, unquoted: not valid YAML, but the CLI reads it
    # through its re-quote retry, so the editor saves it and shows the CLI's description.
    content = _fm("description: Use this: for issues", "argument-hint: [filename] [format]")
    readings = sk._check_skill_md(content)
    assert readings["argument-hint"] == ("str", "[filename] [format]")
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: content}, expected_hash=None)
    (item,) = sk.list_skills(tmp_path)
    assert item["description"] == "Use this: for issues"
    assert "frontmatter_error" not in item


def test_a_tab_indented_skill_header_saves() -> None:
    sk.validate_skill_md_content(_fm("hooks:", "\tPreToolUse: []", "description: d"))


def test_load_does_not_convert_a_header_the_cli_reads_through_its_retry(tmp_path: Path) -> None:
    # The lines on disk are not the lines the CLI parses, so no line is removed.
    raw = _fm("argument-hint: [a] [b]", "shell: 5")
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None and content == raw
    with pytest.raises(sk._IgnoredFieldError):
        sk.validate_skill_md_content(raw)


def test_load_does_not_remove_a_field_a_nested_alias_needs(tmp_path: Path) -> None:
    # Without `shell`, the nested `*s` has no anchor and the text no longer parses at all
    # (the CLI's retry only re-quotes top-level lines), so nothing is converted.
    raw = "---\nshell: &s 5\nmeta:\n  a: *s\n---\nb\n"
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None and content == raw


# --- TASK-482: skill hooks use the hooks surface's validator ---------------------------

_VALID_HOOKS = [
    pytest.param("{type: command, command: ./check.sh, async: true, once: true}", id="command"),
    pytest.param("{type: http, url: 'https://example.com/hook', timeout: 5}", id="http"),
    pytest.param("{type: mcp_tool, server: github, tool: comment, input: {a: 1}}", id="mcp_tool"),
    pytest.param("{type: prompt, prompt: Is this safe, model: haiku}", id="prompt"),
    pytest.param("{type: agent, prompt: Check the tests}", id="agent"),
]


@pytest.mark.parametrize("entry", _VALID_HOOKS)
def test_a_valid_skill_hook_of_every_type_saves(entry: str) -> None:
    sk.validate_skill_md_content(
        _fm("hooks:", "  PreToolUse:", "    - matcher: Bash", f"      hooks: [{entry}]")
    )  # no raise


@pytest.mark.parametrize(
    "entry",
    [
        # A type or field outside the known lists warns (TASK-524); a bad shape is refused.
        pytest.param("{type: 5, command: x}", id="non-string-type"),
        pytest.param("{type: command, command: x, extra: .inf}", id="unknown-field-infinite"),
        pytest.param("{type: http}", id="missing-url"),
        pytest.param("{type: command, command: x, timeout: 0}", id="timeout-not-positive"),
        pytest.param("{type: command, command: '${CLAUDE_PLUGIN_ROOT}/x'}", id="plugin-owned"),
    ],
)
def test_an_invalid_skill_hook_is_refused(entry: str) -> None:
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_md_content(
            _fm("hooks:", "  PreToolUse:", "    - matcher: Bash", f"      hooks: [{entry}]")
        )


def test_a_date_inside_an_mcp_tool_input_is_refused() -> None:
    # A date to PyYAML, a string to YAML 1.2: the two readings of `hooks` differ.
    content = _fm(
        "hooks: {PreToolUse: [{hooks: [{type: mcp_tool, server: s, tool: t, "
        "input: {a: 2024-01-01}}]}]}"
    )
    with pytest.raises(cw.InvalidCandidateError, match="'hooks' reads differently"):
        sk.validate_skill_md_content(content)


def test_a_reader_that_runs_out_of_stack_is_a_422(monkeypatch) -> None:
    # Defence in depth: today the hooks validator bounds every nested hook value, but a
    # reader that overflows the stack must still be a refusal, never a 500.
    def _deep(_value):
        raise RecursionError

    monkeypatch.setattr(sk, "_FIELD_READERS", (("description", _deep),))
    with pytest.raises(cw.InvalidCandidateError, match="nested too deeply to check"):
        sk.validate_frontmatter({"description": "d"})


@pytest.mark.parametrize(
    "hooks",
    [
        pytest.param("hooks:\n  1: []\n  foo: []", id="mixed-event-keys"),
        pytest.param("hooks:\n  ~: []\n  foo: []", id="null-and-text-event-keys"),
        pytest.param("hooks: {Stop: [{1: x, foo: y, hooks: []}]}", id="mixed-group-keys"),
        pytest.param(
            "hooks: {Stop: [{hooks: [{type: command, command: x, 1: a, foo: b}]}]}",
            id="mixed-entry-keys",
        ),
    ],
)
def test_mixed_type_hook_keys_are_a_422_on_every_path(tmp_path: Path, hooks: str) -> None:
    # The hooks validator sorts unknown keys for its message; an int and a str do not sort,
    # and that TypeError used to escape as a 500 (and a 500 for the whole Skills list).
    # Since TASK-524 an unknown TEXT key warns, so the refusal names the key that is not text.
    content = _fm("description: d", hooks)
    with pytest.raises(cw.InvalidCandidateError, match="unknown|text"):
        sk.validate_skill_md_content(content)
    _skill(tmp_path, content)
    (item,) = sk.list_skills(tmp_path)
    assert re.search("unknown|text", item["frontmatter_error"])
    assert sk.read_skill_file_for_edit(tmp_path, "s")[3] is None


# --- TASK-449.6: the seam's composed-tree walks are memoized and iterative -----------------


@pytest.mark.timeout(20)
def test_a_skill_with_a_long_merge_chain_is_read_without_recursion() -> None:
    # 1500 `<<` links load fine in PyYAML and pass the merge-size cap; the YAML 1.2 view and
    # every seam walk follow the whole chain without a stack frame per link.
    links = ["x0: &a0 {k: v}"] + [f"x{i}: &a{i} {{<<: *a{i - 1}}}" for i in range(1, 1500)]
    content = "---\ndescription: d\nmetadata:\n  " + "\n  ".join(links) + "\n---\nb\n"
    assert len(content.encode("utf-8")) < sk.MAX_SKILL_MD_BYTES
    start = time.monotonic()
    sk.validate_skill_md_content(content)  # no raise
    assert time.monotonic() - start < 10.0  # linear; generous for loaded CI runners
    view = cw.yaml12_view(content.split("---\n")[1])
    assert view["metadata"]["x1499"] == {"k": "v"}


@pytest.mark.timeout(20)
def test_a_skill_alias_pyramid_under_a_discarded_key_is_refused_quickly(tmp_path: Path) -> None:
    # PyYAML keeps only the last of two `metadata` keys, so the loaded data is tiny; only a
    # walk of the composed tree sees the pyramid. Each seam walk visits a node once, and the
    # repeated key is refused.
    levels = ["metadata: &p0 [a, a, a, a]"] + [
        f"metadata: &p{i} [*p{i - 1}, *p{i - 1}, *p{i - 1}, *p{i - 1}]" for i in range(1, 21)
    ]
    content = "---\ndescription: d\n" + "\n".join(levels) + "\nmetadata: {}\n---\nb\n"
    assert len(content.encode("utf-8")) < 1500
    start = time.monotonic()
    with pytest.raises(cw.InvalidCandidateError, match="see differently"):
        sk.validate_skill_md_content(content)
    _skill(tmp_path, content)
    (item,) = sk.list_skills(tmp_path)
    assert item["frontmatter_error"]
    assert time.monotonic() - start < 5.0  # a walk per alias path would take hours


# --- Greptile on PR 1810: digit-limit ValueError and nested mappings in text fields ------

_HUGE = "9" * 5000  # past Python's 4300-digit int() limit


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(f'effort: "{_HUGE}"', id="quoted-huge-effort"),
        pytest.param(f'effort: "{_HUGE} tokens"', id="huge-effort-with-text"),
        pytest.param(f"description: 0{_HUGE}", id="leading-zero-huge-number"),
    ],
)
def test_a_huge_number_is_refused_never_a_500(tmp_path: Path, line: str) -> None:
    # JavaScript reads these digits as Infinity, which is not an integer, so the CLI ignores
    # the effort; Python's int() raised ValueError instead, a 500 on write and on every read.
    content = _fm(line)
    with pytest.raises(cw.InvalidCandidateError):
        sk.validate_skill_md_content(content)
    _skill(tmp_path, content, name="bad")
    _skill(tmp_path, _fm("description: fine"), name="good")
    listing = {item["name"]: item for item in sk.list_skills(tmp_path)}
    assert listing["bad"]["frontmatter_error"]
    assert listing["good"]["description"] == "fine"
    sk.read_skill_file_for_edit(tmp_path, "bad")  # no raise


def test_effort_reads_a_number_past_the_double_range_as_ignored() -> None:
    # 400 digits: int() copes, but JavaScript's parseInt gives Infinity, which the CLI's
    # effort reader refuses (not an integer).
    assert sk._cli_effort("9" * 400) is None
    assert sk._cli_effort("9" * 5000) is None
    assert sk._cli_effort(10**400) is None  # a YAML 1.2 hex or octal number this large
    assert sk._cli_effort("12 tokens") == 12


@pytest.mark.parametrize(
    "line",
    [
        "argument-hint: [[{a: 1}]]",
        "argument-hint: [x, [y, [{a: 1}]]]",
        "when_to_use: [[[{a: 1}]]]",
    ],
)
def test_a_mapping_nested_in_lists_is_refused(line: str) -> None:
    # The CLI would show `[object Object]`, however deep the mapping sits in the lists.
    with pytest.raises(cw.InvalidCandidateError, match="must be text"):
        sk.validate_skill_md_content(_fm(line))


def test_nested_lists_of_text_still_save() -> None:
    sk.validate_skill_md_content(_fm("argument-hint: [[a, b], [c, [d]]]"))  # no raise


def test_js_integer_never_converts_more_digits_than_a_double_holds() -> None:
    assert cw.js_integer("9" * 5000) is None
    assert cw.js_integer("9" * 309) is None  # 309 digits, but past 1.8e308
    assert cw.js_integer("-" + "0" * 5000 + "12") == -12
    assert cw.js_integer("+7") == 7


def test_a_huge_hex_key_breaks_neither_the_list_nor_the_editor(tmp_path: Path) -> None:
    # An OverflowError in the duplicate-key check failed the whole listing (review on PR 1810).
    _skill(tmp_path, _fm("x:\n  0x" + "f" * 300 + ": 1"), name="bad")
    _skill(tmp_path, _fm("description: fine"), name="good")
    listing = {item["name"]: item for item in sk.list_skills(tmp_path)}
    assert "nested value" in listing["bad"]["frontmatter_error"]
    assert listing["good"]["description"] == "fine"
    sk.read_skill_file_for_edit(tmp_path, "bad")  # no raise


def test_a_huge_hex_effort_is_refused_as_the_cli_ignores_it() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="effort"):
        sk.validate_skill_md_content(_fm("effort: 0x" + "f" * 300))


# --- Greptile on PR 1810: one file that crashes a check never breaks the listing ----------


def _poison_the_checks(monkeypatch, crash: Exception) -> None:
    real = cw._yaml12_refuses

    def _boom(text, root):
        if "poison" in text:
            raise crash
        return real(text, root)

    monkeypatch.setattr(cw, "_yaml12_refuses", _boom)


def test_route_list_survives_a_value_tagged_key(write_config, tmp_path, projects_root) -> None:
    skills = projects_root / "alpha" / ".claude" / "skills"
    for name, extra in (("bad", "metadata: {=: x}\n"), ("good", "")):
        (skills / name).mkdir(parents=True)
        (skills / name / sk.SKILL_FILENAME).write_bytes(
            f"---\nname: {name}\ndescription: fine\n{extra}---\nbody\n".encode()
        )
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_SKILLS_URL}?project=alpha")
        assert resp.status_code == 200
        listed = {item["name"]: item for item in resp.json()["skills"]}
        assert set(listed) == {"bad", "good"}
        assert listed["good"]["description"] == "fine"
        assert listed["bad"]["description"] == "fine"  # the `=` key reads, as in PyYAML
        assert "frontmatter_error" not in listed["bad"]
        assert c.get(f"{_FILE_URL}?project=alpha&name=bad").status_code == 200


@pytest.mark.parametrize(
    "crash",
    [
        yaml.constructor.ConstructorError("x", None, "y"),
        ValueError("x"),
        OverflowError("x"),
        TypeError("x"),
        RecursionError(),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_route_list_shows_a_file_that_crashes_a_check_as_refused(
    write_config, tmp_path, projects_root, monkeypatch, crash: Exception
) -> None:
    _poison_the_checks(monkeypatch, crash)
    skills = projects_root / "alpha" / ".claude" / "skills"
    for name, extra in (("bad", "x: poison\n"), ("good", "")):
        (skills / name).mkdir(parents=True)
        (skills / name / sk.SKILL_FILENAME).write_bytes(
            f"---\nname: {name}\ndescription: fine\n{extra}---\nbody\n".encode()
        )
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.get(f"{_SKILLS_URL}?project=alpha")
        assert resp.status_code == 200
        listed = {item["name"]: item for item in resp.json()["skills"]}
        assert set(listed) == {"bad", "good"}
        assert listed["bad"]["frontmatter_error"]  # refused, with its reason
        assert listed["good"]["description"] == "fine"
        assert c.get(f"{_FILE_URL}?project=alpha&name=bad").status_code == 200


def test_a_crash_in_the_skills_reading_of_the_view_is_a_refusal(monkeypatch) -> None:
    def _boom(_header):
        raise TypeError("x")

    monkeypatch.setattr(cw, "yaml12_view", _boom)
    with pytest.raises(cw.InvalidCandidateError, match="TypeError"):
        sk.validate_skill_md_content(_fm("description: d"))
    assert sk._ignorable_keys(_fm("effort: 1.5")) is None  # the editor read degrades, no 500


# --- TASK-496: a save keeps every stored member the request does not name ---------------

_RUN_SH = b"#!/bin/sh\necho run\n"
_DEEP_MD = b"# deep\nline two\n"


def _store_skill(skill_dir: Path) -> None:
    """Store a skill by hand: SKILL.md, an executable script, and a nested reference."""
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "refs" / "deep").mkdir(parents=True)
    (skill_dir / sk.SKILL_FILENAME).write_bytes(_VALID_MD.encode())
    (skill_dir / "scripts" / "run.sh").write_bytes(_RUN_SH)
    (skill_dir / "refs" / "deep" / "a.md").write_bytes(_DEEP_MD)
    if os.name == "posix":
        (skill_dir / "scripts" / "run.sh").chmod(0o755)


def _tree(skill_dir: Path) -> dict[str, bytes | None]:
    """Return every member under ``skill_dir`` (a directory maps to None)."""
    return {
        p.relative_to(skill_dir).as_posix(): (None if p.is_dir() else p.read_bytes())
        for p in sorted(skill_dir.rglob("*"))
    }


def test_route_skill_md_save_keeps_the_other_files(write_config, tmp_path, projects_root) -> None:
    skill_dir = projects_root / "alpha" / ".claude" / "skills" / "greeter"
    _store_skill(skill_dir)
    updated = _md(description="updated")
    with _client(write_config, tmp_path, _ON) as c:
        loaded = c.get(f"{_FILE_URL}?project=alpha&name=greeter").json()
        resp = c.put(
            _SKILLS_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "name": "greeter",
                "files": {"SKILL.md": updated},
                "hash": loaded["hash"],
            },
        )
        assert resp.status_code == 200, resp.text
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == updated.encode()
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD
    if os.name == "posix":
        assert (skill_dir / "scripts" / "run.sh").stat().st_mode & 0o777 == 0o755


def _stored(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "skills" / "s"
    _store_skill(skill_dir)
    return skill_dir


def _save(tmp_path: Path, files: dict[str, str], **kwargs: object) -> None:
    kwargs.setdefault("expected_hash", sk._skill_md_hash(tmp_path / "skills", "s"))
    sk.write_skill(tmp_path, "s", files, **kwargs)  # type: ignore[arg-type]


def test_write_skill_named_member_replaces_its_stored_copy(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/run.sh": "echo new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == b"echo new\n"
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD


def test_write_skill_keeps_nested_hidden_and_empty_members(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    (skill_dir / ".env.example").write_bytes(b"A=1\r\nB=2\r\n")  # CRLF kept as stored
    (skill_dir / "assets" / "empty").mkdir(parents=True)
    (skill_dir / "refs" / "deep" / "b.bin").write_bytes(bytes(range(256)))
    if os.name == "posix":
        (skill_dir / "refs").chmod(0o700)
    before = _tree(skill_dir)
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    after = _tree(skill_dir)
    assert after.pop(sk.SKILL_FILENAME) == _md(description="new").encode()
    before.pop(sk.SKILL_FILENAME)
    assert after == before
    if os.name == "posix":
        assert (skill_dir / "refs").stat().st_mode & 0o777 == 0o700
    _assert_no_orphans(tmp_path / "skills")


def _assert_no_orphans(skills_root: Path) -> None:
    assert [p.name for p in skills_root.iterdir() if p.name.startswith(".")] == []


def test_write_skill_cap_counts_kept_files(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _stored(tmp_path)
    before = _tree(skill_dir)
    kept = len(_RUN_SH) + len(_DEEP_MD)
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", kept + len(_VALID_MD) - 1)
    with pytest.raises(cw.InvalidCandidateError, match=r"would be over the .* byte cap: the save"):
        _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD})
    assert _tree(skill_dir) == before
    _assert_no_orphans(tmp_path / "skills")
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", kept + len(_VALID_MD))
    _save(tmp_path, {sk.SKILL_FILENAME: _VALID_MD})  # exactly at the cap is allowed


def test_write_skill_refuses_when_kept_files_alone_exceed_the_cap(
    tmp_path: Path, monkeypatch
) -> None:
    skill_dir = _stored(tmp_path)
    before = _tree(skill_dir)
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", len(_RUN_SH))
    with pytest.raises(
        cw.InvalidCandidateError, match="too large together.* no stored file was dropped"
    ):
        _save(tmp_path, {sk.SKILL_FILENAME: "x"})
    assert _tree(skill_dir) == before  # refused whole: no member dropped to fit


def test_write_skill_does_not_count_a_replaced_member_twice(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _stored(tmp_path)
    # Kept a.md + the two sent files fit; counting the stored run.sh as well would not.
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", len(_DEEP_MD) + len(_VALID_MD) + 2)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "scripts//run.sh": "ok"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == b"ok"


@needs_symlink
@pytest.mark.parametrize("as_dir", [False, True], ids=["file-link", "dir-link"])
def test_write_skill_refuses_a_stored_symlink_and_changes_nothing(
    tmp_path: Path, as_dir: bool
) -> None:
    skill_dir = _stored(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret\n")
    link = skill_dir / "refs" / "link"
    link.symlink_to(outside if as_dir else outside / "secret", target_is_directory=as_dir)
    before_md = (skill_dir / sk.SKILL_FILENAME).read_bytes()
    with pytest.raises(cw.InvalidCandidateError, match="'refs/link' is a symlink"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == before_md
    assert link.is_symlink()
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH
    assert (outside / "secret").read_bytes() == b"secret\n"
    _assert_no_orphans(tmp_path / "skills")


def _swap_after_listing(monkeypatch, listed: Path, swap) -> None:
    """Run ``swap()`` right after the walk lists the stored directory ``listed``."""
    real = sk._list_stored_dir

    def _listing_then_swap(path, expected, member, **kwargs):
        out = real(path, expected, member, **kwargs)
        if member == listed:
            swap()
        return out

    monkeypatch.setattr(sk, "_list_stored_dir", _listing_then_swap)


@needs_symlink
def test_write_skill_never_copies_through_a_dir_swapped_for_a_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    # The race: after the walk lists refs/deep, another local writer replaces `refs` with
    # a symlink to an outside tree of the same shape. The open of refs/deep/a.md then
    # resolves outside, and the identity check refuses it instead of copying the secret.
    skill_dir = _stored(tmp_path)
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (outside / "deep" / "a.md").write_bytes(b"OUTSIDE SECRET\n")

    def _swap() -> None:
        (skill_dir / "refs").rename(tmp_path / "moved-refs")
        (skill_dir / "refs").symlink_to(outside, target_is_directory=True)

    _swap_after_listing(monkeypatch, Path("refs/deep"), _swap)
    with pytest.raises(cw.InvalidCandidateError, match="'refs/deep/a.md' changed while"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()  # no promote
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_refuses_a_dir_replaced_after_the_listing(tmp_path: Path, monkeypatch) -> None:
    skill_dir = _stored(tmp_path)

    def _swap() -> None:
        (skill_dir / "refs" / "deep").rename(tmp_path / "moved-deep")
        (skill_dir / "refs" / "deep").mkdir()  # same path, another directory

    _swap_after_listing(monkeypatch, Path("refs"), _swap)
    with pytest.raises(cw.InvalidCandidateError, match="'refs/deep' changed while"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    _assert_no_orphans(tmp_path / "skills")


def _open_fds() -> int | None:
    fd_dir = Path("/proc/self/fd")
    return len(list(fd_dir.iterdir())) if fd_dir.is_dir() else None


def _grow_in_place(a_md: Path, _moved: Path) -> None:
    with a_md.open("ab") as fh:  # the same file, so only the size check sees it
        fh.write(b"grown\n")


def _replace_with(make):
    def _replace(a_md: Path, moved: Path) -> None:
        a_md.rename(moved)
        make(a_md)

    return _replace


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_grow_in_place, id="grown-in-place"),
        pytest.param(_replace_with(lambda p: p.mkdir()), id="now-a-dir"),
        pytest.param(_replace_with(lambda p: p.write_bytes(_DEEP_MD)), id="now-another-file"),
        pytest.param(_replace_with(lambda p: None), id="removed"),
        pytest.param(
            _replace_with(lambda p: (p.write_bytes(b"x"), p.chmod(0o000))),
            id="now-an-unreadable-file",
            marks=pytest.mark.skipif(
                os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits, non-root"
            ),
        ),
        pytest.param(
            _replace_with(lambda p: p.symlink_to(p.parent / "elsewhere")),
            id="now-a-symlink",
            marks=needs_symlink,
        ),
        pytest.param(
            _replace_with(lambda p: os.mkfifo(p)),
            id="now-a-fifo",
            marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo"),
        ),
    ],
)
def test_write_skill_refuses_a_file_changed_after_the_listing(
    tmp_path: Path, monkeypatch, change
) -> None:
    # a.md grew, was removed, or another file, a directory, a symlink or a FIFO now sits
    # where the listing saw it. Each gives the same refusal on every OS (on Windows the
    # open of a directory fails with PermissionError first). The open never blocks
    # (O_NONBLOCK), leaks no descriptor, and the save refuses before a promote.
    skill_dir = _stored(tmp_path)
    a_md = skill_dir / "refs" / "deep" / "a.md"
    _swap_after_listing(
        monkeypatch, Path("refs/deep"), lambda: change(a_md, tmp_path / "moved-a.md")
    )
    before = _open_fds()
    with pytest.raises(cw.InvalidCandidateError, match="'refs/deep/a.md' changed while"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert _open_fds() == before
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    _assert_no_orphans(tmp_path / "skills")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_write_skill_clears_setuid_on_a_kept_file(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    (skill_dir / "scripts" / "run.sh").chmod(0o4755)
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / "scripts" / "run.sh").stat().st_mode & 0o7777 == 0o755
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows feature")
def test_write_skill_refuses_a_stored_junction(tmp_path: Path) -> None:
    import _winapi

    skill_dir = _stored(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret\n")
    _winapi.CreateJunction(str(outside), str(skill_dir / "refs" / "j"))
    with pytest.raises(cw.InvalidCandidateError, match="'refs/j' is a symlink"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    _assert_no_orphans(tmp_path / "skills")


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits and a non-root user"
)
def test_write_skill_keeps_a_read_only_dir_manageable(tmp_path: Path) -> None:
    # A stored 0o555 directory keeps its group and other bits, but its owner gets write
    # back, so a later save can remove the old tree instead of leaving it as trash.
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs" / "deep").chmod(0o555)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/new.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "deep" / "new.md").read_bytes() == b"new\n"
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD
    assert (skill_dir / "refs" / "deep").stat().st_mode & 0o777 == 0o755
    # The first save's trash still holds the 0o555 directory (behavior from before this
    # change, see replace_tree); the next save must not add another one.
    trash_after_first = sorted((tmp_path / "skills").glob(".s.trash-*"))
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="again")})
    assert sorted((tmp_path / "skills").glob(".s.trash-*")) == trash_after_first
    for trash in trash_after_first:  # let pytest remove the tmp dir
        for d in [trash, *trash.rglob("*")]:
            if d.is_dir():
                d.chmod(0o755)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo (POSIX)")
def test_write_skill_refuses_a_stored_fifo_without_reading_it(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    os.mkfifo(skill_dir / "pipe")  # a copy would block forever reading it
    with pytest.raises(cw.InvalidCandidateError, match="'pipe' is a symlink or not a regular"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits and a non-root user"
)
@pytest.mark.parametrize("member", ["scripts/run.sh", "refs"])
def test_write_skill_refuses_a_stored_member_it_cannot_read(tmp_path: Path, member: str) -> None:
    skill_dir = _stored(tmp_path)
    (skill_dir / member).chmod(0o000)
    try:
        with pytest.raises(cw.InvalidCandidateError, match="could not be kept as stored"):
            _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    finally:
        (skill_dir / member).chmod(0o755)
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_stale_hash_refuses_and_changes_nothing(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    before = _tree(skill_dir)
    with pytest.raises(cw.StaleConfigWriteError):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="x")}, expected_hash="0" * 64)
    assert _tree(skill_dir) == before
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_rechecks_the_hash_in_build(tmp_path: Path, monkeypatch) -> None:
    # A writer that changes SKILL.md after the fail-fast check is caught by the re-check
    # in `build`, and its bytes survive. That `build` runs inside replace_tree's lock is
    # test_replace_tree_runs_build_inside_the_target_lock in test_config_file_writer.py.
    skill_dir = _stored(tmp_path)
    real_replace_tree = sk.fw.replace_tree

    def _racing_replace_tree(root, relative, build):
        (skill_dir / sk.SKILL_FILENAME).write_bytes(b"concurrent\n")
        return real_replace_tree(root, relative, build)

    monkeypatch.setattr(sk.fw, "replace_tree", _racing_replace_tree)
    with pytest.raises(cw.StaleConfigWriteError, match="while it was saved"):
        _save(tmp_path, {sk.SKILL_FILENAME: _md(description="x")})
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == b"concurrent\n"
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH


@pytest.mark.parametrize(
    ("member", "match"),
    [("refs/deep", "is a directory"), ("scripts/run.sh/x", "holds as a file")],
)
def test_write_skill_refuses_a_member_that_collides_with_the_stored_tree(
    tmp_path: Path, member: str, match: str
) -> None:
    skill_dir = _stored(tmp_path)
    before = _tree(skill_dir)
    with pytest.raises(cw.InvalidCandidateError, match=match):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _VALID_MD, member: "x"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    assert _tree(skill_dir) == before
    _assert_no_orphans(tmp_path / "skills")


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe-496"
    probe.write_bytes(b"")
    try:
        return (directory / "CASE-PROBE-496").exists()
    finally:
        probe.unlink()


def test_write_skill_replaces_a_read_only_member_named_in_another_case(tmp_path: Path) -> None:
    # macOS and Windows: `scripts/RUN.sh` names the stored `scripts/run.sh`, so that
    # read-only file is replaced: never copied, never written through (a 500 before).
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    skill_dir = _stored(tmp_path)
    (skill_dir / "scripts" / "run.sh").chmod(0o444)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/RUN.sh": "echo new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "scripts" / "RUN.sh").read_bytes() == b"echo new\n"


def test_write_skill_near_cap_replacement_in_another_case_fits(
    tmp_path: Path, monkeypatch
) -> None:
    # Greptile P2. On a case-insensitive file system, `scripts/RUN.sh` replaces the stored
    # `scripts/run.sh`, so the stored bytes must not count: kept a.md + sent fits exactly.
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    skill_dir = _stored(tmp_path)
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", len(_DEEP_MD) + len(_VALID_MD) + 2)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/RUN.sh": "ok"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "scripts" / "RUN.sh").read_bytes() == b"ok"
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD


def _as_if_case_insensitive(monkeypatch, skill_dir: Path, sent: str, stored: str) -> None:
    """Make the walk see ``sent`` resolve to the stored ``stored``, as APFS or NTFS would."""
    real_lstat = sk.os.lstat

    def _lstat(path, *args, **kwargs):
        if Path(path) == skill_dir / sent:
            path = skill_dir / stored
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(sk.os, "lstat", _lstat)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits and a non-root user"
)
def test_write_skill_matches_a_member_by_the_file_systems_own_name_rules(
    tmp_path: Path, monkeypatch
) -> None:
    # Linux stand-in for the case above: the file system answers that `scripts/RUN.sh` is
    # the stored `scripts/run.sh`. The stored copy is neither counted nor opened, so a
    # near-cap save fits, and an unreadable old copy does not block its replacement.
    skill_dir = _stored(tmp_path)
    (skill_dir / "scripts" / "run.sh").chmod(0o000)
    _as_if_case_insensitive(monkeypatch, skill_dir, "scripts/RUN.sh", "scripts/run.sh")
    monkeypatch.setattr(sk, "MAX_TOTAL_BYTES", len(_DEEP_MD) + len(_VALID_MD) + 2)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "scripts/RUN.sh": "ok"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    # The new text keeps the stored file's mode, 0o000 too (TASK-500).
    assert (skill_dir / "scripts" / "RUN.sh").stat().st_mode & 0o7777 == 0o000
    (skill_dir / "scripts" / "RUN.sh").chmod(0o644)
    assert (skill_dir / "scripts" / "RUN.sh").read_bytes() == b"ok"
    # The listing, not exists(): on APFS `run.sh` "exists" whenever `RUN.sh` does.
    assert os.listdir(skill_dir / "scripts") == ["RUN.sh"]  # replaced, as on APFS or NTFS


def _hard_link_or_skip(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except OSError:
        pytest.skip("hard links (under this name) unavailable")


@pytest.mark.parametrize(
    ("stored", "other", "sent"),
    [
        ("run.sh", "RUN.sh", "RUN.sh"),  # case
        ("é.md", "é.md", "é.md"),  # NFC and NFD
    ],
    ids=["case", "normalization"],
)
def test_write_skill_keeps_a_differently_spelled_hard_link_when_both_names_are_stored(
    tmp_path: Path, stored: str, other: str, sent: str
) -> None:
    # On a file system that keeps both spellings apart (ext4), two names are two members,
    # even as hard links of one file: sending one must not drop the other.
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs" / stored).write_bytes(b"kept\n")
    if (skill_dir / "refs" / other).exists():
        pytest.skip("this file system does not keep both spellings apart")
    _hard_link_or_skip(skill_dir / "refs" / stored, skill_dir / "refs" / other)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, f"refs/{sent}": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert sorted(os.listdir(skill_dir / "refs")) == sorted(["deep", stored, other])
    assert (skill_dir / "refs" / stored).read_bytes() == b"kept\n"
    assert (skill_dir / "refs" / sent).read_bytes() == b"new\n"


@pytest.mark.parametrize(("stored", "sent"), [("run.sh", "RUN.sh"), ("ss.md", "ß.md")])
def test_write_skill_keeps_a_separate_file_that_only_folds_to_the_sent_name(
    tmp_path: Path, stored: str, sent: str
) -> None:
    # A fold alone never replaces: where the file system keeps the names apart (ext4 for
    # case, NTFS and APFS for ss and ß), the stored file is a different member and stays.
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs" / stored).write_bytes(b"kept\n")
    if (skill_dir / "refs" / sent).exists():
        pytest.skip("this file system treats the two names as one")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, f"refs/{sent}": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / stored).read_bytes() == b"kept\n"
    assert (skill_dir / "refs" / sent).read_bytes() == b"new\n"


@needs_symlink
def test_write_skill_refuses_a_key_through_a_stored_symlink_loop(tmp_path: Path) -> None:
    # Python 3.11 and 3.12 raise RuntimeError from Path.resolve() on a loop. The lookup of
    # the keys never resolves, so the walk refuses the link with a 422, never a 500.
    skill_dir = _stored(tmp_path)
    (skill_dir / "loop").symlink_to("loop")
    with pytest.raises(cw.InvalidCandidateError, match="'loop' is a symlink"):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _VALID_MD, "loop/x.md": "x\n"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_keeps_a_hard_link_of_a_replaced_member(tmp_path: Path) -> None:
    # The same file under another name is a different member: replacing a.md keeps b.md.
    skill_dir = _stored(tmp_path)
    try:
        os.link(skill_dir / "refs" / "deep" / "a.md", skill_dir / "refs" / "deep" / "b.md")
    except OSError:
        pytest.skip("hard links unavailable")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/a.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == b"new\n"
    assert (skill_dir / "refs" / "deep" / "b.md").read_bytes() == _DEEP_MD


def _during_named_write(monkeypatch, member: str, action) -> None:
    """Run ``action(staging_path)`` right before the build creates a named member's file."""
    real_create = sk._create_exclusive

    def _create(path, data):
        if ".staging-" in str(path) and Path(path).as_posix().endswith(member):
            action(Path(path))
        return real_create(path, data)

    monkeypatch.setattr(sk, "_create_exclusive", _create)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_write_skill_keeps_staging_private_until_the_promote(tmp_path: Path, monkeypatch) -> None:
    # Greptile P1 (a). A stored 0o777 directory must not make staging writable by another
    # user while the build still writes: staging and every directory in it are 0o700 then.
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs").chmod(0o777)
    seen: dict[str, int] = {}

    def _record(target: Path) -> None:
        staging = next(p for p in target.parents if ".staging-" in p.name)
        for rel in ("", "refs", "refs/deep", "refs/fresh"):
            seen[rel] = (staging / rel).stat().st_mode & 0o7777

    # refs/fresh is new: it is made for the named member, and is owner-only as well.
    _during_named_write(monkeypatch, "refs/fresh/new.md", _record)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/fresh/new.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert seen == {"": 0o700, "refs": 0o700, "refs/deep": 0o700, "refs/fresh": 0o700}
    assert (skill_dir / "refs").stat().st_mode & 0o777 == 0o777  # its final mode, at promote
    (tmp_path / "default-mode").mkdir()  # a new directory's mode under this umask
    default = (tmp_path / "default-mode").stat().st_mode & 0o777
    assert (skill_dir / "refs" / "fresh").stat().st_mode & 0o777 == default
    assert (skill_dir / "refs" / "fresh" / "new.md").read_bytes() == b"new\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_write_skill_never_takes_group_or_world_write_on_the_skill_dir(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    skill_dir.chmod(0o3777)  # setgid + sticky + group and world write
    (skill_dir / "refs").chmod(0o1777)  # world-writable and sticky, like /tmp
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    # setgid and the group and world write go. The sticky bit stays: on refs, it is what
    # stops another user from renaming over or deleting the owner's files.
    assert skill_dir.stat().st_mode & 0o7777 == 0o1755
    assert (skill_dir / "refs").stat().st_mode & 0o7777 == 0o1777


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_write_skill_sets_final_dir_modes_deepest_first(tmp_path: Path, monkeypatch) -> None:
    # os.chmod follows symlinks in the path, so each directory above the one it changes
    # must still be owner-only: the root opens last.
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs").chmod(0o755)
    real_chmod = sk.os.chmod
    checked: list[str] = []

    def _chmod(path, mode, *args, **kwargs):
        path = Path(path)
        staging = next((p for p in [path, *path.parents] if ".staging-" in p.name), None)
        if staging is not None and path.is_dir():
            for above in path.parents:
                if above == staging.parent:
                    break
                assert above.stat().st_mode & 0o777 == 0o700, (path, above)
            checked.append(path.relative_to(staging).as_posix())
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(sk.os, "chmod", _chmod)
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    assert checked[-1] == "."  # the root last
    assert set(checked) == {".", "refs", "refs/deep", "scripts"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
def test_write_skill_new_dirs_take_the_umask(tmp_path: Path) -> None:
    old = os.umask(0o077)
    try:
        sk.write_skill(
            tmp_path,
            "s",
            {sk.SKILL_FILENAME: _VALID_MD, "fresh/x.md": "x\n"},
            expected_hash=None,
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    finally:
        os.umask(old)
    skill_dir = tmp_path / "skills" / "s"
    assert skill_dir.stat().st_mode & 0o777 == 0o700
    assert (skill_dir / "fresh").stat().st_mode & 0o777 == 0o700


def _another_group() -> int | None:
    if os.name != "posix":
        return None
    others = [g for g in os.getgroups() if g != os.getegid()]
    return others[0] if others else None


def _chown_or_skip(path: Path, gid: int | None) -> None:
    # A runner's group list (opendirectoryd on macOS) may name a group chown refuses.
    try:
        os.chown(path, -1, gid)
    except PermissionError:
        pytest.skip(f"cannot give a file group {gid} here")


@pytest.mark.skipif(_another_group() is None, reason="needs a second group the user is in")
def test_write_skill_keeps_the_stored_group(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    gid = _another_group()
    for member in ("refs", "scripts/run.sh"):
        _chown_or_skip(skill_dir / member, gid)
        (skill_dir / member).chmod(0o770)
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    for member in ("refs", "scripts/run.sh"):
        info = (skill_dir / member).stat()
        assert (info.st_gid, info.st_mode & 0o777) == (gid, 0o770)


@pytest.mark.skipif(_another_group() is None, reason="needs a second group the user is in")
def test_write_skill_clears_group_bits_it_cannot_keep_for_the_stored_group(
    tmp_path: Path, monkeypatch
) -> None:
    # When the stored group cannot be set back, the group bits go: they never pass to the
    # group the new copy happens to get (`staff` on macOS holds every local account).
    skill_dir = _stored(tmp_path)
    gid = _another_group()
    for member in ("refs", "scripts/run.sh"):
        _chown_or_skip(skill_dir / member, gid)
        (skill_dir / member).chmod(0o770)

    def _no_chown(*_args, **_kwargs):
        raise PermissionError("not a member")

    monkeypatch.setattr(sk.os, "chown", _no_chown)
    _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    for member in ("refs", "scripts/run.sh"):
        assert (skill_dir / member).stat().st_mode & 0o777 == 0o700


def test_write_skill_a_key_reaching_a_stored_dir_replaces_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    # Only a stored FILE can be replaced: a key that the file system maps to a stored
    # directory (`Refs` reaching `refs` on APFS or NTFS) leaves the directory and its files.
    # Simulated on a case-sensitive file system; the real case is the test below.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates a case-insensitive lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    _as_if_case_insensitive(monkeypatch, skill_dir, "Refs", "refs")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "Refs": "x\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD


def test_write_skill_refuses_a_key_that_is_a_stored_dir_in_another_case(tmp_path: Path) -> None:
    # macOS and Windows: `Refs` IS the stored directory `refs`, so a file there would
    # replace the directory and its files. The save refuses, and the directory stays.
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    skill_dir = _stored(tmp_path)
    with pytest.raises(cw.InvalidCandidateError, match="'Refs' is a directory in the skill"):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _VALID_MD, "Refs": "x\n"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_keeps_a_file_whose_name_folds_to_the_key_but_is_not_what_it_maps_to(
    tmp_path: Path, monkeypatch
) -> None:
    # The file system maps the key `SS.md` to another file (`other.md`, as an NTFS 8.3
    # short name maps to its long name), not to the stored `ss.md` whose name folds to it.
    # Neither a fold nor the identity alone is proof: both files stay.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs" / "ss.md").write_bytes(b"kept\n")
    (skill_dir / "refs" / "other.md").write_bytes(b"other\n")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/SS.md", "refs/other.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/SS.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "ss.md").read_bytes() == b"kept\n"
    assert (skill_dir / "refs" / "other.md").read_bytes() == b"other\n"


def test_write_skill_keeps_every_hard_link_when_the_key_spells_one(
    tmp_path: Path, monkeypatch
) -> None:
    # `a.md` and `b.md` are hard links of one file, and the key `A.md` maps to `a.md`.
    # Identity cannot tell hard links apart, so neither is taken as replaced: both stay.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    deep = skill_dir / "refs" / "deep"
    _hard_link_or_skip(deep / "a.md", deep / "b.md")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/deep/A.md", "refs/deep/a.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/A.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert sorted(os.listdir(deep)) == ["A.md", "a.md", "b.md"]
    assert (deep / "a.md").read_bytes() == _DEEP_MD
    assert (deep / "b.md").read_bytes() == _DEEP_MD


def test_write_skill_keeps_a_hard_link_named_like_an_8dot3_alias(
    tmp_path: Path, monkeypatch
) -> None:
    # NTFS: `ssnamelong.md` has the short name `SSNAME~1.MD`, and `ßname~1.md` is a hard
    # link of it under a separate name. The key `SSNAME~1.MD` reaches the shared inode, and
    # only `ßname~1.md` folds to it, but the key never names that file. It must stay.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    refs = skill_dir / "refs"
    (refs / "ssnamelong.md").write_bytes(b"long\n")
    _hard_link_or_skip(refs / "ssnamelong.md", refs / "ßname~1.md")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/SSNAME~1.MD", "refs/ssnamelong.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/SSNAME~1.MD": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (refs / "ßname~1.md").read_bytes() == b"long\n"
    assert (refs / "ssnamelong.md").read_bytes() == b"long\n"


def test_write_skill_keeps_a_file_with_a_hard_link_elsewhere(tmp_path: Path, monkeypatch) -> None:
    # The only entry in its directory, but it has another hard link elsewhere: a link that
    # appears between the listing and the lookup looks the same, so it is not proof.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    deep = skill_dir / "refs" / "deep"
    _hard_link_or_skip(deep / "a.md", skill_dir / "refs" / "elsewhere.md")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/deep/A.md", "refs/deep/a.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/A.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (deep / "a.md").read_bytes() == _DEEP_MD


def test_write_skill_matches_nothing_on_a_file_system_without_inodes(
    tmp_path: Path, monkeypatch
) -> None:
    # Some SMB or FUSE mounts report st_ino 0 for every file, so every entry looks like
    # the same object. Identity then proves nothing, and the stored file stays.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    deep = skill_dir / "refs" / "deep"
    real_lstat = sk.os.lstat
    redirect = {deep / "A.md": deep / "a.md"}

    def _lstat(path, *args, **kwargs):
        path = redirect.get(Path(path), path)
        info = real_lstat(path, *args, **kwargs)
        if Path(path).parent == deep:
            fields = list(info[:10])
            fields[1] = 0  # st_ino
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(sk.os, "lstat", _lstat)
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/A.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (deep / "a.md").read_bytes() == _DEEP_MD


def test_write_skill_keeps_the_file_when_the_key_reaches_nothing_in_the_listing(
    tmp_path: Path, monkeypatch
) -> None:
    # A race: the lookup of the key reaches an object that is not in the listing it just
    # read. That proves nothing either: no match (never an IndexError, a 500), file kept.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside\n")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/deep/A.md", "../../outside.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/deep/A.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "deep" / "a.md").read_bytes() == _DEEP_MD


def test_write_skill_refuses_two_keys_for_one_path(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    with pytest.raises(
        cw.InvalidCandidateError, match="'refs//x.md' names a file that is already in the save"
    ):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _VALID_MD, "refs/x.md": "1\n", "refs//x.md": "2\n"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    assert not (skill_dir / "refs" / "x.md").exists()
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_refuses_a_key_that_collides_with_a_kept_copy(tmp_path: Path) -> None:
    # macOS and Windows: `scripts/RUN.sh` reaches the stored `scripts/run.sh`, but run.sh
    # has another hard link, so the save cannot prove that the key names it. run.sh is
    # kept, the key then collides with its copy in staging, and the save refuses.
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive file system")
    skill_dir = _stored(tmp_path)
    _hard_link_or_skip(skill_dir / "scripts" / "run.sh", skill_dir / "refs" / "link.sh")
    with pytest.raises(cw.InvalidCandidateError, match="names a file that is already in the save"):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _VALID_MD, "scripts/RUN.sh": "new\n"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_keeps_both_names_when_a_key_fits_two_hard_links(
    tmp_path: Path, monkeypatch
) -> None:
    # NTFS or APFS: `ss.md` and `ß.md` are two names (hard links of one file), and the
    # key `SS.md` maps to `ss.md`. Both names fold to the key, so the save cannot tell
    # which one the key names, and keeps both rather than drop one.
    if _case_insensitive(tmp_path):
        pytest.skip("simulates the lookup on a case-sensitive file system")
    skill_dir = _stored(tmp_path)
    (skill_dir / "refs" / "ss.md").write_bytes(b"kept\n")
    _hard_link_or_skip(skill_dir / "refs" / "ss.md", skill_dir / "refs" / "ß.md")
    _as_if_case_insensitive(monkeypatch, skill_dir, "refs/SS.md", "refs/ss.md")
    _save(
        tmp_path,
        {sk.SKILL_FILENAME: _VALID_MD, "refs/SS.md": "new\n"},
        confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
    )
    assert (skill_dir / "refs" / "ss.md").read_bytes() == b"kept\n"
    assert (skill_dir / "refs" / "ß.md").read_bytes() == b"kept\n"


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0, reason="needs POSIX mode bits and a non-root user"
)
def test_write_skill_reports_the_open_error_when_the_recheck_cannot_look(
    tmp_path: Path, monkeypatch
) -> None:
    # The open fails (EACCES), and the lstat that checks for a change fails for another
    # reason than "gone": that is not a change, so the open's own error is reported.
    skill_dir = _stored(tmp_path)
    run_sh = skill_dir / "scripts" / "run.sh"
    run_sh.chmod(0o000)
    real_lstat = sk.os.lstat

    def _lstat(path, *args, **kwargs):
        if Path(path) == run_sh:
            raise OSError(5, "lstat I/O error")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(sk.os, "lstat", _lstat)
    try:
        with pytest.raises(
            cw.InvalidCandidateError, match="could not be kept as stored: Permission denied"
        ):
            _save(tmp_path, {sk.SKILL_FILENAME: _md(description="new")})
    finally:
        run_sh.chmod(0o755)


@pytest.mark.skipif(_open_fds() is None, reason="needs /proc/self/fd to count descriptors")
def test_create_exclusive_closes_the_descriptor_when_fdopen_fails(
    tmp_path: Path, monkeypatch
) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("fdopen failed")

    monkeypatch.setattr(sk.os, "fdopen", _boom)
    before = _open_fds()
    with pytest.raises(OSError, match="fdopen failed"):
        sk._create_exclusive(tmp_path / "new.md", b"x")
    assert _open_fds() == before


@needs_symlink
def test_write_skill_refuses_a_symlink_planted_at_a_named_path(
    tmp_path: Path, monkeypatch
) -> None:
    # Greptile P1 (b). A symlink planted after the lexists check and before the write makes the
    # save refuse. The write never follows it, and nothing is promoted.
    skill_dir = _stored(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    _during_named_write(monkeypatch, "refs/planted.md", lambda p: p.symlink_to(outside))
    with pytest.raises(cw.InvalidCandidateError, match="'refs/planted.md' changed while"):
        _save(
            tmp_path,
            {sk.SKILL_FILENAME: _md(description="new"), "refs/planted.md": "mine\n"},
            confirm_scripts=sk.SCRIPT_CONFIRM_TOKEN,
        )
    assert outside.read_bytes() == b"outside\n"
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()
    _assert_no_orphans(tmp_path / "skills")


def test_write_skill_create_keeps_files_of_a_dir_without_skill_md(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "s"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.sh").write_bytes(_RUN_SH)
    sk.write_skill(tmp_path, "s", {sk.SKILL_FILENAME: _VALID_MD}, expected_hash=None)
    assert (skill_dir / "scripts" / "run.sh").read_bytes() == _RUN_SH
    assert (skill_dir / sk.SKILL_FILENAME).read_bytes() == _VALID_MD.encode()


def test_delete_skill_still_removes_every_member(tmp_path: Path) -> None:
    skill_dir = _stored(tmp_path)
    assert sk.delete_skill(tmp_path, "s") is True
    assert not skill_dir.exists()
