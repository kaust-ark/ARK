"""ARK orchestrator package.

Re-exports ``Orchestrator`` and ``main`` from .core so existing callers
(``from ark.orchestrator import Orchestrator``) keep working after the
single-file → package refactor.
"""
from .core import Orchestrator, main

__all__ = ["Orchestrator", "main"]
