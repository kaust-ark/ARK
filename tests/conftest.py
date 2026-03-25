"""Shared fixtures for ARK tests."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure ARK package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Provide a temporary state directory for memory/state tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def tmp_figures_dir(tmp_path):
    """Provide a temporary figures directory."""
    fig_dir = tmp_path / "figures"
    fig_dir.mkdir()
    return fig_dir


@pytest.fixture(autouse=True)
def _reset_memory_singleton():
    """Reset the memory singleton before and after each test."""
    import ark.memory
    ark.memory._memory = None
    yield
    ark.memory._memory = None
