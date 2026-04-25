"""ARK orchestrator package.

Re-exports ``Orchestrator`` and ``main`` from .core so existing callers
(``from ark.orchestrator import Orchestrator``) keep working after the
single-file → package refactor.

``ARK_ROOT`` is a module-level constant several tests patch via
``mock.patch("ark.orchestrator.ARK_ROOT", …)``; we re-export it here so
the patch target keeps resolving on the package namespace, not just the
internal ``.core`` submodule.
"""
from .core import Orchestrator, main, ARK_ROOT

__all__ = ["Orchestrator", "main", "ARK_ROOT"]
