"""Verify the installed package can be imported."""

import importlib


def test_package_import_loads_installed_module() -> None:
    """The editable installation exposes the expected package name."""
    package = importlib.import_module('incidents_agent')

    assert package.__name__ == 'incidents_agent'
