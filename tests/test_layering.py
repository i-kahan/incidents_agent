"""Packages may import only downward, so the import graph stays acyclic."""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / 'src' / 'incidents_agent'

# Each package and the packages it must never import (directly or by name).
FORBIDDEN = {
    'llm': {'ingest', 'tools', 'memory', 'agent', 'eval'},
    'ingest': {'tools', 'memory', 'agent', 'eval'},
    'memory': {'tools', 'agent', 'eval'},
    'tools': {'agent', 'eval'},
    'agent': {'eval'},
}


def _imported_packages(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        names: list[str] = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            parts = name.split('.')
            if parts[0] == 'incidents_agent' and len(parts) > 1:
                found.add(parts[1])
    return found


@pytest.mark.parametrize('package', sorted(FORBIDDEN))
def test_package_does_not_import_upward(package: str) -> None:
    """A lower layer never reaches back into one built on top of it."""
    offences = {
        str(path.relative_to(SRC)): sorted(
            _imported_packages(path) & FORBIDDEN[package]
        )
        for path in sorted((SRC / package).glob('*.py'))
        if _imported_packages(path) & FORBIDDEN[package]
    }

    assert not offences
