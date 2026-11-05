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
import shlex
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
        {"good-name": "bogus-value"},
        {"good-name": True},
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


def test_route_overrides_bad_value_is_422_and_writes_nothing(
    write_config, tmp_path, projects_root
) -> None:
    with _client(write_config, tmp_path, _ON) as c:
        resp = c.put(
            _OVERRIDES_URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "overrides": {"greeter": "bogus"},
            },
        )
        assert resp.status_code == 422
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()


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
                "overrides": {"greeter": "bogus"},
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
                "overrides": {"greeter": "bogus"},
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

    real_read_bytes = Path.read_bytes

    def _boom(self):
        if self == bad_md:
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)

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

    def _boom(self):
        raise OSError("device fell off the bus")

    monkeypatch.setattr(Path, "read_bytes", _boom)

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
    pytest.param("effort: banana", True, id="effort-unknown"),
    pytest.param("effort: ' high'", True, id="effort-not-trimmed"),
    pytest.param("effort: true", True, id="effort-bool"),
    pytest.param("model: ''", True, id="model-blank"),
    pytest.param("model: 5", True, id="model-number"),
    pytest.param("context: forked", True, id="context-other"),
    pytest.param("context: Fork", True, id="context-case"),
    pytest.param("shell: zsh", True, id="shell-unknown"),
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
        "---\ndescription: d\nshell: zsh  # my shell\neffort: banana\n"
        "metadata:\n  - not\n  - a map\nname: kept\n---\nBody.\n"
    )
    path = _skill(tmp_path, raw)
    content, file_hash, exists, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert exists
    assert content == "---\ndescription: d\nname: kept\n---\nBody.\n"
    assert file_hash == cw.hash_bytes(path.read_bytes())  # the file ON DISK, for the 409 guard
    assert note is not None
    for key in ("'shell'", "'effort'", "'metadata'"):
        assert key in note
    sk.validate_skill_md_content(content)  # the converted text saves
    # The plain read never converts: it is the file as it is on disk.
    assert sk.read_skill_file(tmp_path, "s")[0] == raw


def test_load_keeps_a_bom_and_crlf_line_ends_when_it_converts(tmp_path: Path) -> None:
    _skill(tmp_path, "\ufeff---\r\ndescription: d\r\ncontext: forked\r\n---\r\nBody.\r\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == "\ufeff---\r\ndescription: d\r\n---\r\nBody.\r\n"
    assert note is not None


def test_load_converts_the_last_field_without_a_blank_line(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\nshell: zsh\n---\nBody.\n")
    content, _, _, _ = sk.read_skill_file_for_edit(tmp_path, "s")
    assert content == "---\ndescription: d\n---\nBody.\n"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("---\ndescription: d\n---\nBody.\n", id="nothing-to-convert"),
        pytest.param("---\nhooks: [x]\n---\nBody.\n", id="hooks-not-droppable"),
        pytest.param("---\nuser-invocable: maybe\n---\nb\n", id="user-invocable-not-droppable"),
        pytest.param("---\n{shell: zsh}\n---\nb\n", id="flow-style-root"),
        pytest.param("---\nshell: zsh\nshell: bash\n---\nb\n", id="duplicate-key"),
        pytest.param("---\neffort: x\neffort: y\n---\nb\n", id="duplicate-ignored-key"),
        pytest.param("---\n  description: d\n  shell: zsh\n---\nb\n", id="indented-root"),
        pytest.param("---\nx: &x {shell: zsh}\n<<: *x\n---\nb\n", id="field-from-a-merge"),
        pytest.param("---\nshell: zsh\neffort: 010\n---\nb\n", id="also-a-mismatch"),
        pytest.param("---\nnever closed\n", id="unparseable"),
    ],
)
def test_load_never_converts_what_it_cannot_prove_equivalent(tmp_path: Path, raw: str) -> None:
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None
    assert content == raw


def test_load_redacts_after_converting(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\napi_key: sk-not-a-real-key-0000\nshell: zsh\n---\nb\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is not None
    assert "sk-not-a-real-key-0000" not in content
    assert "shell" not in content


def test_load_does_not_convert_a_supporting_file(tmp_path: Path) -> None:
    _skill(tmp_path, "---\ndescription: d\n---\nb\n")
    (tmp_path / "skills" / "s" / "notes.md").write_bytes(b"---\nshell: zsh\n---\n")
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s", "notes.md")
    assert note is None and "shell: zsh" in content


def test_route_returns_the_converted_text_and_saves_it(
    write_config, tmp_path, projects_root
) -> None:
    raw = "---\ndescription: d\neffort: banana\n---\nBody.\n"
    path = projects_root / "alpha" / ".claude" / "skills" / "greeter" / sk.SKILL_FILENAME
    path.parent.mkdir(parents=True)
    # Byte-exact LF: text mode writes CRLF on Windows, and the route returns the file's
    # own line endings (Windows CI, PR 1810).
    path.write_bytes(raw.encode("utf-8"))
    with _client(write_config, tmp_path, _ON) as c:
        view = c.get(f"{_FILE_URL}?project=alpha&name=greeter").json()
        assert view["converted"] and "'effort'" in view["converted"]
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
        "hooks: {Unknown: [{matcher: x, hooks: [{type: command, command: y}]}]}",
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
    fields = ["effort: banana", "shell: zsh", "context: forked", "model: ''", "created_by: me"]
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
    raw = _fm("argument-hint: [a] [b]", "shell: zsh")
    _skill(tmp_path, raw)
    content, _, _, note = sk.read_skill_file_for_edit(tmp_path, "s")
    assert note is None and content == raw
    with pytest.raises(sk._IgnoredFieldError):
        sk.validate_skill_md_content(raw)


def test_load_does_not_remove_a_field_a_nested_alias_needs(tmp_path: Path) -> None:
    # Without `shell`, the nested `*s` has no anchor and the text no longer parses at all
    # (the CLI's retry only re-quotes top-level lines), so nothing is converted.
    raw = "---\nshell: &s zsh\nmeta:\n  a: *s\n---\nb\n"
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
        pytest.param("{type: shell, command: x}", id="unknown-type"),
        pytest.param("{type: command, command: x, extra: 1}", id="unknown-field"),
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
    content = _fm("description: d", hooks)
    with pytest.raises(cw.InvalidCandidateError, match="unknown"):
        sk.validate_skill_md_content(content)
    _skill(tmp_path, content)
    (item,) = sk.list_skills(tmp_path)
    assert "unknown" in item["frontmatter_error"]
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
