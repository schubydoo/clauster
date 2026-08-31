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


def test_validate_frontmatter_requires_description() -> None:
    with pytest.raises(cw.InvalidCandidateError, match="description"):
        sk.validate_frontmatter({})


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
        {"description": "x", "name": "../escape"},
        {"description": "x", "name": 5},
        {"description": "x", "disable-model-invocation": "true"},
        {"description": "x", "user-invocable": "false"},
        {"description": "x", "allowed-tools": 5},
        {"description": "x", "argument-hint": 5},
        {"description": ""},
        {"description": "   "},
        {"description": 5},
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
            tmp_path, "my-skill", {sk.SKILL_FILENAME: "no frontmatter"}, expected_hash=None
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


def test_list_skills_redacts_secret_in_frontmatter_error(tmp_path: Path) -> None:
    # A YAML parse error can echo a fragment of the offending line back; a secret
    # interpolation in that fragment must be masked in the error string too.
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    # Malformed YAML (unbalanced bracket) whose error message quotes the secret line.
    (bad / sk.SKILL_FILENAME).write_bytes(
        b"---\ndescription: [unclosed ${SUPER_SECRET_VALUE}\n---\nbody"
    )
    listing = sk.list_skills(tmp_path)
    err = listing[0]["frontmatter_error"]
    assert "SUPER_SECRET_VALUE" not in err
    assert cw.REDACTION_SENTINEL in err


def test_list_skills_reports_frontmatter_error_without_raising(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    bad = skills_root / "broken"
    bad.mkdir(parents=True)
    (bad / sk.SKILL_FILENAME).write_bytes(b"no frontmatter here")
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
        {"../escape": "on"},
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
                "files": {"SKILL.md": "no frontmatter"},
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
                "files": {"SKILL.md": "no frontmatter"},
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
