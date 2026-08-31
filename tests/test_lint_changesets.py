"""Tests for scripts/lint_changesets.py — the frontmatter (#1320) + one-line (#599) guards."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "scripts" / "lint_changesets.py"
_KNOPE_TOML = _REPO / "knope.toml"
_spec = importlib.util.spec_from_file_location("lint_changesets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lint_changesets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_changesets)

# The package keys / change types knope.toml actually sanctions. Read from the real file so
# these tests exercise the repo's own layout rather than a hand-copied duplicate of it.
_PACKAGES, _CHANGE_TYPES = lint_changesets._knope_expectations(str(_KNOPE_TOML))


def _violation(raw: str) -> str | None:
    """Run the linter's full per-file check (frontmatter then body) on a changeset string."""
    front, body = lint_changesets._split_frontmatter(raw)
    return lint_changesets._frontmatter_violation(
        "x.md", front, _PACKAGES, _CHANGE_TYPES
    ) or lint_changesets._violation("x.md", body)


# --- body rule (#599): one line, always renders as a bullet -------------------------------


def test_single_line_body_passes() -> None:
    assert _violation("---\ndefault: patch\n---\n\nFix the thing, all on one clean line.") is None


def test_multi_paragraph_body_rejected() -> None:
    # The #599 breakage: a blank-line-separated "Upgrade note" makes knope render the entry
    # as a #### heading block instead of a bullet.
    body = "---\ndefault: minor\n---\n\nAdd a feature that does X.\n\nUpgrade note: also do Y."
    msg = _violation(body)
    assert msg is not None
    assert "multiple lines" in msg


def test_multiline_with_complete_first_line_now_rejected() -> None:
    # Previously ALLOWED (line 1 ended with '.', deemed a "clean heading"); now rejected —
    # a heading among bullets is itself the breakage this guard exists to prevent.
    body = "---\ndefault: patch\n---\n\nA complete summary sentence.\nTrailing detail line."
    assert _violation(body) is not None


def test_empty_body_rejected() -> None:
    assert _violation("---\ndefault: patch\n---\n") is not None


# --- frontmatter rule (#1320): knope silently skips a fragment it can't key ----------------


def test_knope_expectations_match_this_repos_layout() -> None:
    # clauster has a single unnamed [package], so knope names it `default`; the custom
    # sections in knope.toml contribute their change types alongside the built-in bumps.
    assert _PACKAGES == frozenset({"default"})
    assert {"major", "minor", "patch", "perf", "security", "build"} <= _CHANGE_TYPES


@pytest.mark.parametrize("bump", ["major", "minor", "patch", "perf", "security", "build"])
def test_every_sanctioned_change_type_passes(bump: str) -> None:
    # `perf` / `security` / `build` come from knope.toml's extra_changelog_sections and have
    # shipped in real releases — a linter that only allowed major/minor/patch would break them.
    assert _violation(f"---\ndefault: {bump}\n---\n\nA clean one-line summary.") is None


def test_wrong_package_key_rejected() -> None:
    # The #1320 bug itself: `clauster: patch` instead of `default: patch`. knope skipped
    # three such fragments and the 1.1.0 changelog was missing their entries.
    msg = _violation("---\nclauster: patch\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "'clauster'" in msg


def test_extra_key_alongside_default_rejected() -> None:
    # knope 0.23.0 does cope with this one — the linter is deliberately stricter, because a
    # stray second key is a typo and the next fragment may not keep the valid key.
    msg = _violation("---\ndefault: patch\nclauster: patch\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "'clauster'" in msg


# Verified against knope 0.23.0: each of these bumps the version and DELETES the fragment
# while writing an empty changelog section — the entry is lost with the evidence.
@pytest.mark.parametrize(
    "bump", ["pathc", '"patch"', "'patch'", "Patch", "MINOR", "patch # why", "docs"]
)
def test_unusable_change_type_rejected(bump: str) -> None:
    msg = _violation(f"---\ndefault: {bump}\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "not valid for this repo" in msg


def test_missing_frontmatter_rejected() -> None:
    msg = _violation("A clean one-line summary with no frontmatter at all.")
    assert msg is not None
    assert "frontmatter" in msg


def test_unterminated_frontmatter_rejected() -> None:
    msg = _violation("---\ndefault: patch\n\nA clean one-line summary.")
    assert msg is not None
    assert "frontmatter" in msg


def test_empty_frontmatter_rejected() -> None:
    msg = _violation("---\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "empty frontmatter" in msg


def test_unparsable_frontmatter_line_rejected() -> None:
    msg = _violation("---\ndefault patch\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "unparsable" in msg


def test_blank_line_inside_frontmatter_rejected() -> None:
    # knope rejects it with `invalid front matter`; skipping it here would only defer that
    # failure to the release run.
    msg = _violation("---\ndefault: patch\n\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "unparsable" in msg


def test_duplicate_frontmatter_key_rejected() -> None:
    msg = _violation("---\ndefault: bogus\ndefault: patch\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "duplicate" in msg


def test_crlf_fragment_accepted() -> None:
    # Windows checkouts: splitlines() normalizes CRLF, and knope accepts such a fragment.
    assert _violation("---\r\ndefault: patch\r\n---\r\n\r\nA clean one-line summary.") is None


# Shapes knope 0.23.0 accepts and renders as a normal entry. Pinned as PASSING so a future
# tightening of the hand-rolled parser can't start rejecting real changesets unnoticed —
# a false positive here blocks every PR in the repo.
@pytest.mark.parametrize(
    "raw",
    [
        "---\ndefault:patch\n---\n\nNo space after the colon.",
        "---\n  default: patch\n---\n\nIndented frontmatter line.",
        "---\ndefault:\tpatch\n---\n\nTab after the colon.",
        "---\ndefault: patch   \n---\n\nTrailing spaces on the frontmatter line.",
        "---   \ndefault: patch\n---   \n\nTrailing spaces on the fences.",
        "---\ndefault: patch\n---\nNo blank line after the frontmatter.",
        "---\ndefault: patch\n---\n\nNo trailing newline.",
        "---\ndefault: patch\n---\n\n\nLeading blank lines before the summary.\n\n\n",
    ],
)
def test_shapes_knope_accepts_are_not_rejected(raw: str) -> None:
    assert _violation(raw) is None


def test_byte_order_mark_rejected() -> None:
    # A BOM makes the first bytes something other than `---`; knope reports `missing front
    # matter` and never reads the fragment. Spelled as an escape — an inline U+FEFF would be
    # invisible in the source and the next reader would "fix" the test by deleting it.
    msg = _violation(chr(0xFEFF) + "---\ndefault: patch\n---\n\nA clean one-line summary.")
    assert msg is not None
    assert "frontmatter" in msg


def test_multi_package_layout_is_refused_rather_than_guessed(tmp_path) -> None:
    # knope scopes extra_changelog_sections per package in this layout; the linter does not
    # model that, so it must fail loudly instead of blessing a fragment knope would drop.
    cfg = tmp_path / "knope.toml"
    cfg.write_text('[packages.alpha]\nchangelog = "CHANGELOG.md"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="multi-package"):
        lint_changesets._knope_expectations(str(cfg))


def test_knope_toml_without_a_package_table_is_refused(tmp_path) -> None:
    cfg = tmp_path / "knope.toml"
    cfg.write_text('[github]\nowner = "schubydoo"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="no `\\[package\\]` table"):
        lint_changesets._knope_expectations(str(cfg))


# --- main(): the exit code CI gates on ----------------------------------------------------


def _write(path: Path, text: str) -> None:
    """Write a fixture fragment with byte-exact \\n, so Windows runs the same bytes."""
    path.write_text(text, encoding="utf-8", newline="")


def test_main_exit_codes(tmp_path, monkeypatch) -> None:
    # Cover main() itself — the exit code CI gates on. A refactor that returns 0 for a
    # violating file would slip past the helper-only tests above (Greptile P2).
    cdir = tmp_path / ".changeset"
    cdir.mkdir()
    _write(cdir / "good.md", "---\ndefault: patch\n---\n\nA clean one-line summary.\n")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", cdir)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 0  # all fragments well-formed -> pass
    _write(cdir / "bad.md", "---\ndefault: patch\n---\n\nLine one.\n\nLine two.\n")
    assert lint_changesets.main() == 1  # a multi-line fragment -> fail
    (cdir / "bad.md").unlink()
    _write(cdir / "miskeyed.md", "---\nclauster: patch\n---\n\nOne line.\n")
    assert lint_changesets.main() == 1  # a fragment knope would skip -> fail


def test_main_finds_fragments_under_a_path_containing_glob_metacharacters(
    tmp_path, monkeypatch
) -> None:
    # `glob.glob` would take the whole anchored path as a PATTERN, so a checkout under a
    # directory named e.g. `br[1]` matched nothing and reported "0 fragment(s) OK" — the
    # silent pass this linter exists to close, reintroduced by the anchoring itself.
    cdir = tmp_path / "br[1]" / ".changeset"
    cdir.mkdir(parents=True)
    _write(cdir / "miskeyed.md", "---\nclauster: patch\n---\n\nThe exact #1320 bug.\n")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", cdir)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 1


def test_main_fails_closed_when_knope_toml_is_unreadable(tmp_path, monkeypatch) -> None:
    # Without knope.toml the linter cannot know which keys knope accepts; passing anyway is
    # how a silently-skipped fragment shipped in the first place.
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(tmp_path / "nope.toml"))
    assert lint_changesets.main() == 1


def test_relative_path_label_falls_back_to_the_absolute_path(monkeypatch) -> None:
    # os.path.relpath raises on Windows when cwd sits on a different drive than the repo;
    # the report must still name the file rather than blowing up the whole lint run.
    def _boom(_path: str) -> str:
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(lint_changesets.os.path, "relpath", _boom)
    assert lint_changesets._relative_to_cwd("/repo/.changeset/a.md") == "/repo/.changeset/a.md"


def test_main_fails_closed_on_a_layout_it_cannot_model(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "knope.toml"
    cfg.write_text('[packages.alpha]\nchangelog = "CHANGELOG.md"\n', encoding="utf-8")
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(cfg))
    assert lint_changesets.main() == 1


def test_main_reports_an_unreadable_fragment(tmp_path, monkeypatch) -> None:
    cdir = tmp_path / ".changeset"
    cdir.mkdir()
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", cdir)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    (cdir / "gone.md").mkdir()  # a directory matching the glob -> open() raises OSError
    assert lint_changesets.main() == 1


def test_main_rejects_a_changeset_readme(tmp_path, monkeypatch) -> None:
    # knope 0.23.0 has no README exemption — it parses `.changeset/README.md` as a fragment
    # and dies on `missing front matter`, so the linter must not exempt it either.
    cdir = tmp_path / ".changeset"
    cdir.mkdir()
    _write(cdir / "README.md", "# Changesets\n\nHow to write one.\n")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", cdir)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 1


# --- layout rule (#1332): knope reads `.changeset/*.md` and nothing else -------------------


@pytest.fixture
def changeset_dir(tmp_path, monkeypatch) -> Path:
    """Point the linter at a throwaway `.changeset/` holding one valid fragment.

    Strays must never be committed into the repo's real `.changeset/` — that would trip
    this very linter (and knope) on every branch.
    """
    cdir = tmp_path / ".changeset"
    cdir.mkdir()
    _write(cdir / "good.md", "---\ndefault: patch\n---\n\nA clean one-line summary.\n")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", cdir)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    return cdir


def test_a_lone_valid_fragment_passes(changeset_dir: Path) -> None:
    # The control: the fixture's own fragment must pass, or the failures below prove nothing.
    assert lint_changesets.main() == 0


@pytest.mark.parametrize(
    "name", [".gitkeep", ".DS_Store", "Thumbs.db", ".good.md.swp", ".good.md.swn", "a~"]
)
def test_housekeeping_entries_are_allowed(changeset_dir: Path, name: str, capsys) -> None:
    # `.gitkeep` keeps the directory in git; `.DS_Store`/`Thumbs.db` appear from merely
    # OPENING the folder on macOS/Windows and a vim swap file exists while a fragment is
    # being edited — none is a lost changeset, and failing on them would red only local runs.
    # `.swn` pins the swap-name CASCADE (`.swp` → `.swo` → `.swn` → …), which vim reaches
    # whenever a stale swap file from a crash still occupies the earlier name.
    # Asserting the COUNT too: a regression that counted these as fragments would still
    # exit 0, and the point is that they are not fragments.
    _write(changeset_dir / name, "")
    assert lint_changesets.main() == 0
    assert "1 fragment(s) OK" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["a.txt", "a.MD", "a.md.bak", "changeset", "a.markdown"])
def test_non_md_stray_file_rejected(changeset_dir: Path, name: str, capsys) -> None:
    # A fragment that lands under the wrong extension is invisible to knope — no changelog
    # entry, no version bump, no error. `a.MD` is rejected on every OS: the release runs on
    # Linux, where the glob is case-sensitive and the file is simply dropped.
    _write(changeset_dir / name, "---\ndefault: patch\n---\n\nA clean one-line summary.\n")
    assert lint_changesets.main() == 1
    out = capsys.readouterr().out
    # The PATH, not the bare name: for the extensionless "changeset" param the bare name
    # is already boilerplate in every failure line, so it proves nothing about which
    # file was flagged.
    assert f"{changeset_dir.name}{os.sep}{name}" in out
    assert "not a `*.md` fragment" in out


def test_nested_fragment_rejected(changeset_dir: Path, capsys) -> None:
    # knope's `*.md` glob does not recurse, so `.changeset/sub/a.md` is dropped silently.
    sub = changeset_dir / "sub"
    sub.mkdir()
    _write(sub / "a.md", "---\ndefault: patch\n---\n\nA clean one-line summary.\n")
    assert lint_changesets.main() == 1
    out = capsys.readouterr().out
    assert "sub: a directory" in out
    assert "does not recurse" in out


def test_stray_is_reported_alongside_a_valid_fragment(changeset_dir: Path, capsys) -> None:
    # The stray must not be masked by the valid fragment sitting next to it (the realistic
    # shape: a PR adds one good changeset and one misnamed one).
    _write(changeset_dir / "a.txt", "---\ndefault: patch\n---\n\nA clean one-line summary.\n")
    assert lint_changesets.main() == 1
    assert "a.txt" in capsys.readouterr().out


def test_changeset_path_that_is_not_a_directory_is_rejected(tmp_path, monkeypatch, capsys) -> None:
    # The one shape where the "absent is fine" allowance could become this script's OWN
    # silent pass: a regular file at `.changeset` would otherwise report 0 fragments OK.
    stub = tmp_path / ".changeset"
    _write(stub, "not a directory\n")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", stub)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 1
    assert "not a directory" in capsys.readouterr().out


def test_dangling_changeset_symlink_is_rejected(tmp_path, monkeypatch, capsys) -> None:
    # `Path.exists()` follows the link, so a dead `.changeset -> /nowhere` reads as "absent"
    # and would take the legitimate release-checkout pass — the same silent pass by a
    # slightly different route.
    link = tmp_path / ".changeset"
    try:
        link.symlink_to(tmp_path / "nowhere", target_is_directory=True)
    except (OSError, NotImplementedError):  # Windows without developer mode / admin
        pytest.skip("this platform does not permit creating symlinks")
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", link)
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 1
    assert "not a directory" in capsys.readouterr().out


def test_unlistable_changeset_dir_is_reported_not_a_traceback(
    changeset_dir: Path, monkeypatch, capsys
) -> None:
    # A permission error on the directory must produce the script's own message, like every
    # other error path here, rather than exiting on a bare traceback.
    def _boom(_self) -> None:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(lint_changesets.Path, "iterdir", _boom)
    assert lint_changesets.main() == 1
    assert "could not list" in capsys.readouterr().out


def test_absent_changeset_dir_passes(tmp_path, monkeypatch, capsys) -> None:
    # knope deletes every fragment while preparing a release and git does not track the
    # emptied directory, so an absent `.changeset/` is legitimate — not a stray to fail on.
    monkeypatch.setattr(lint_changesets, "_CHANGESET_DIR", tmp_path / "nope")
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(_KNOPE_TOML))
    assert lint_changesets.main() == 0
    assert "0 fragment(s) OK" in capsys.readouterr().out


def test_main_fails_closed_on_a_misshapen_knope_toml(tmp_path, monkeypatch) -> None:
    # A wrong SHAPE (not just wrong syntax) must still produce the message rather than a
    # bare traceback: `section.get(...)` on a string raises AttributeError, not ValueError.
    cfg = tmp_path / "knope.toml"
    cfg.write_text('[package]\nextra_changelog_sections = "oops"\n', encoding="utf-8")
    monkeypatch.setattr(lint_changesets, "_KNOPE_TOML", str(cfg))
    assert lint_changesets.main() == 1
