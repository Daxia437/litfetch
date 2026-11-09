"""The skill ships inside the wheel, so it cannot drift from the CLI again."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from litfetch import cli  # noqa: E402

PACKAGED = Path(__file__).resolve().parents[1] / "litfetch" / "skill" / "SKILL.md"


def _run(*argv):
    args = cli.build_parser().parse_args(list(argv))
    return args.func(args)


def test_the_packaged_skill_is_what_the_cli_reads():
    assert cli._skill_text().encode("utf-8") == PACKAGED.read_bytes()


def test_install_then_check_reports_identical(tmp_path):
    assert _run("skill", "--target", str(tmp_path)) == 0
    installed = tmp_path / "lit-fetch" / "SKILL.md"
    assert installed.exists()
    assert installed.read_bytes() == PACKAGED.read_bytes()
    assert _run("skill", "--target", str(tmp_path), "--check") == 0


def test_check_detects_a_local_edit(tmp_path):
    """An older installed copy is exactly the drift this command exists to catch."""
    _run("skill", "--target", str(tmp_path))
    installed = tmp_path / "lit-fetch" / "SKILL.md"
    installed.write_text(installed.read_text(encoding="utf-8") + "\nlocal edit\n",
                         encoding="utf-8")
    assert _run("skill", "--target", str(tmp_path), "--check") == 1


def test_the_skill_version_is_the_package_version():
    """The README promises one version number for the CLI and the skill.

    `skill --check` compares bytes, so it stayed silent while the front matter
    advertised 3.3.0 inside a 1.1.0 wheel. Renumbering the package is exactly
    when that happens, so the equality is asserted here instead.
    """
    import re

    from litfetch import __version__

    text = PACKAGED.read_text(encoding="utf-8")
    m = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m, "SKILL.md has no version: line in its front matter"
    assert m.group(1) == __version__, (
        f"SKILL.md says {m.group(1)}, the package says {__version__}")


def test_check_fails_when_nothing_is_installed(tmp_path):
    assert _run("skill", "--target", str(tmp_path), "--check") == 1


def test_install_is_idempotent(tmp_path):
    _run("skill", "--target", str(tmp_path))
    before = (tmp_path / "lit-fetch" / "SKILL.md").stat().st_mtime_ns
    _run("skill", "--target", str(tmp_path))
    after = (tmp_path / "lit-fetch" / "SKILL.md").stat().st_mtime_ns
    assert before == after          # "up to date" must not rewrite the file


def test_schema_command_writes_a_parseable_file(tmp_path):
    out = tmp_path / "record.schema.json"
    assert _run("schema", "--write", str(out)) == 0
    import json
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["$schema"].startswith("https://json-schema.org/")
