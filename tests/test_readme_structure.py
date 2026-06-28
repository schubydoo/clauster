from pathlib import Path

README = Path("README.md")


def test_readme_install_teaser_precedes_features() -> None:
    text = README.read_text(encoding="utf-8")

    assert "**Install now:**" in text
    assert "## Features" in text
    assert text.index("**Install now:**") < text.index("## Features")


def test_opt_in_extras_are_collapsed() -> None:
    text = README.read_text(encoding="utf-8")

    assert "<details>" in text
    assert "<summary>Opt-in extras</summary>" in text
    assert "### Opt-in extras" not in text
    assert "<summary>Opt-in extras</summary>\n\n" in text
    assert "\n\n</details>" in text
