"""Structural guard: ground truth must never reach the matcher.

If any module under ``ledgerloop/`` can read ``truth_links.csv``, then every metric
this project reports is worthless, and the failure would be invisible in the output —
the numbers would simply look excellent.

This test is the reason the numbers can be trusted. Do not weaken it, do not add
exemptions, and if it fails, fix the import rather than the test.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "ledgerloop"

FORBIDDEN_TOKENS = ("truth_links", "ground_truth", "truth_link")

#: The generator is the one part of ``ledgerloop/`` that legitimately mentions ground
#: truth — it *writes* truth_links.csv. Writing it is fine; reading it back into a
#: matching decision is the leak. Everything downstream of ingest stays clean.
#:
#: Keep this exemption to exactly this one subpackage. If you find yourself wanting to
#: add another entry, that is the leak announcing itself.
TRUTH_AUTHORING_DIRS = {"generate"}


def _python_files() -> list[pathlib.Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _matcher_files() -> list[pathlib.Path]:
    return [
        path
        for path in _python_files()
        if not TRUTH_AUTHORING_DIRS.intersection(path.relative_to(PACKAGE_ROOT).parts)
    ]


def test_package_has_python_files() -> None:
    """Guards the guard: an empty glob would make every test below vacuously pass."""
    assert _python_files(), "no python files found under ledgerloop/"


@pytest.mark.parametrize("path", _matcher_files(), ids=lambda p: p.name)
def test_matcher_never_references_ground_truth(path: pathlib.Path) -> None:
    source = path.read_text()
    for token in FORBIDDEN_TOKENS:
        assert token not in source, (
            f"{path.relative_to(PACKAGE_ROOT.parent)} references {token!r}. "
            "Ground truth belongs to eval/ alone."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_matcher_never_imports_eval_package(path: pathlib.Path) -> None:
    """The eval package reads ground truth, so importing it is a transitive leak."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("eval."), f"{path.name} imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("eval"), f"{path.name} imports from {module}"


def test_cascade_is_covered_by_the_token_check() -> None:
    """Guards the exemption above.

    If someone widens ``TRUTH_AUTHORING_DIRS`` to include the cascade, the token test
    would still pass while checking nothing that matters. This asserts the tier
    modules remain inside the checked set.
    """
    checked = {p.name for p in _matcher_files()}
    for required in ("tier0_exact.py", "tier1_tolerant.py", "tier2_subsetsum.py", "tier3_llm.py"):
        assert required in checked, f"{required} escaped the ground-truth check"
