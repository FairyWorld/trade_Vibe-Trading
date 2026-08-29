"""Regression tests: every ``references/`` link in data-source SKILL.md files
resolves through the ``read_file`` tool.

Background:
    ``read_file`` roots reads at the bundled ``skills/`` directory. The
    skill-name prefix (e.g. ``tushare/references/...``) is what these files are
    written with, and these tests lock that convention in. A bare
    ``references/...`` is now resolved against the skill that owns it as well,
    so the form a human follows on GitHub reaches the same file — see
    ``test_read_file_skill_relative.py`` for that resolution path.

No live API is touched: ``read_file`` performs local filesystem reads of the
bundled skill docs only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from src.tools.read_file_tool import ReadFileTool

# Bundled skills root (mirrors ReadFileTool's own allowed-root computation).
_SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "skills"

# Markdown link whose target is a references/*.md or scripts/*.py path, e.g.
# "[label](tushare/references/foo/bar.md)" or "[ex](sec-edgar/scripts/x.py)".
# The target itself may contain parentheses (some tushare filenames do, e.g.
# "社融增量(月度).md"), so anchor on the trailing ".md)"/".py)" rather than the
# first ")". It must not contain "]" or a newline, though: excluding only "("
# let a match start at one link's "](" and run through the prose to a later
# link's ".py)", capturing a paragraph as a single "target".
_MD_LINK_RE = re.compile(
    r"\]\((?P<target>[^\]\n]*?(?:references/|scripts/)[^\]\n]*?\.(?:md|py))\)"
)


def _skills_with_reference_links() -> Tuple[str, ...]:
    """Return every bundled skill whose SKILL.md links into its own tree.

    Discovered, never listed. A hand-written tuple named five skills and let the
    others drift out of coverage: ``chanlun`` and ``ashare-pre-st-filter``
    shipped 8 links with no prefix — unreachable to the agent — precisely
    because nothing was looking at them.
    """
    return tuple(
        sorted(
            path.parent.name
            for path in _SKILLS_DIR.rglob("SKILL.md")
            if _MD_LINK_RE.search(path.read_text(encoding="utf-8"))
        )
    )


#: Skills whose SKILL.md links into a references/ and/or scripts/ tree.
_SKILLS_UNDER_TEST = _skills_with_reference_links()


def _extract_reference_links(skill: str) -> List[str]:
    """Return every markdown link target containing ``references/``.

    Args:
        skill: Skill directory name (e.g. ``tushare``).

    Returns:
        List of raw link targets as written in SKILL.md.
    """
    text = (_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    return [m.group("target") for m in _MD_LINK_RE.finditer(text)]


def _read(path: str) -> dict:
    """Resolve a path through the read_file tool and return the parsed body.

    Args:
        path: Path argument passed to read_file (no run_dir; skills/ root).

    Returns:
        Parsed JSON response from ReadFileTool.execute.
    """
    return json.loads(ReadFileTool().execute(path=path))


def _all_links() -> List[Tuple[str, str]]:
    """Collect (skill, link) pairs across all skills under test."""
    pairs: List[Tuple[str, str]] = []
    for skill in _SKILLS_UNDER_TEST:
        for link in _extract_reference_links(skill):
            pairs.append((skill, link))
    return pairs


def test_skills_have_reference_links() -> None:
    """Sanity: each skill under test exposes references/ links to validate."""
    for skill in _SKILLS_UNDER_TEST:
        assert _extract_reference_links(skill), f"{skill} has no references/ links"


def test_every_skill_with_reference_links_is_covered() -> None:
    """Discovery must find every such skill, not a subset someone typed out.

    The parametrised tests below are only as wide as this set. While it was a
    literal tuple, a skill could add prefix-less links and stay green forever —
    which is exactly what two of them did.
    """
    linking = {
        path.parent.name
        for path in _SKILLS_DIR.rglob("SKILL.md")
        if _MD_LINK_RE.search(path.read_text(encoding="utf-8"))
    }
    assert set(_SKILLS_UNDER_TEST) == linking
    assert len(_all_links()) == sum(
        len(_extract_reference_links(skill)) for skill in linking
    )


@pytest.mark.parametrize("skill,link", _all_links())
def test_reference_links_carry_skill_prefix(skill: str, link: str) -> None:
    """Every references/ or scripts/ link is written with its skill-name prefix.

    The prefixed form names one file outright. The bare form now resolves too,
    but by searching the skills tree, which a future same-named reference in a
    second skill would make ambiguous — so this is still the form to write.
    """
    assert link.startswith(f"{skill}/"), (
        f"{skill}/SKILL.md link must carry the '{skill}/' prefix, got: {link}"
    )


@pytest.mark.parametrize("skill,link", _all_links())
def test_reference_links_resolve_through_read_file(skill: str, link: str) -> None:
    """Every references/ link resolves to an existing file via read_file."""
    body = _read(link)
    assert body["status"] == "ok", f"{link} did not resolve: {body}"
    assert body["content"], f"{link} resolved to empty content"


def test_bare_reference_link_resolves_to_the_same_file() -> None:
    """A bare references/ path (no prefix) reaches the same document.

    This is the form GitHub resolves for a human reading the SKILL.md, so both
    consumers of the link have to land on one file.
    """
    skill, link = _all_links()[0]
    bare = link[len(f"{skill}/"):]  # strip the skill-name prefix
    body = _read(bare)
    assert body["status"] == "ok", f"{bare} did not resolve: {body}"
    assert body["path"] == _read(link)["path"]
