"""The agent skill in skills/pool follows the Agent Skills format and matches the real `pool` command.

Format rules: https://agentskills.io/specification (read 6 November 2026). Matching the command keeps the skill
from teaching flags or subcommands that do not exist, or calling refused flags allowed.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from pool_cli import main as cli
from pool_cli import run as pool_run

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "pool"
SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def _split() -> tuple[dict, str]:
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert text.startswith("---\n"), "the frontmatter must start on the first line"
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def test_frontmatter_follows_the_spec():
    front, body = _split()
    assert set(front) <= SPEC_FIELDS, f"only the spec's fields are portable: {set(front) - SPEC_FIELDS}"
    name = front["name"]
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    assert name == SKILL_DIR.name, "name must match the folder"
    assert 1 <= len(front["description"]) <= 1024
    assert "Use when" in front["description"], "the description says when to use the skill"
    assert len(front.get("compatibility", "")) <= 500
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in front.get("metadata", {}).items())
    assert len((SKILL_DIR / "SKILL.md").read_text().splitlines()) < 500


def test_linked_files_exist_one_level_deep():
    _, body = _split()
    links = re.findall(r"\]\(([^)#]+)\)", body)
    assert links
    for link in links:
        assert not link.startswith(("/", "http")), link
        assert link.count("/") <= 1, f"keep references one level deep: {link}"
        assert (SKILL_DIR / link).is_file(), link
    for script in ("check.sh", "wait.sh"):
        assert f"scripts/{script}" in body
        assert (SKILL_DIR / "scripts" / script).stat().st_mode & 0o111, f"{script} must be executable"


def _skill_text() -> str:
    return "\n".join(p.read_text() for p in sorted(SKILL_DIR.rglob("*.md")))


def test_pool_run_flags_in_the_skill_exist():
    known = pool_run._VALUE_OPTS | {"-d", "--detach", "--local", "-y", "--yes", "--help", "--"}
    refused = set(pool_run.REFUSED) | pool_run.LATER
    _, body = _split()
    # Every flag in a `pool run ...` example must be one pool run takes (or one the text says it refuses).
    for line in re.findall(r"`pool run[^`]*`", _skill_text()):
        own = line.split(" -- ", 1)[0]  # words after `--` are the program's own arguments
        for flag in re.findall(r"(?<![\w-])(--?[a-z][\w-]*)", own):
            assert flag in known or flag in refused or flag in pool_run.NOTED, f"unknown pool run flag {flag}: {line}"
    # The refused list in SKILL.md is really refused, and -it really is accepted.
    para = body.split("`pool run` refuses", 1)[1].split("The message says why", 1)[0]
    for flag in re.findall(r"`(-[\w-]+)`", para):
        assert flag in refused, f"{flag} is not refused by pool run"
    assert "-it" in pool_run.NOTED


def test_subcommands_in_the_skill_exist():
    commands = {c.name or c.callback.__name__ for c in cli.app.registered_commands}
    groups = {g.name for g in cli.app.registered_groups}
    for sub in set(re.findall(r"`pool ([a-z]+)", _skill_text())):
        assert sub in commands | groups, f"pool {sub} is not a pool command"
