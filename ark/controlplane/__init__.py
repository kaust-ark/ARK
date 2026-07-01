"""Control-plane boundary for the orchestrator.

The orchestrator talks to the control plane exclusively through
``ControlPlaneClient`` (see CONTROL_PLANE_BOUNDARY.md). Build one with
``build_client`` and use it as the single seam — no direct ``website.dashboard.db``
access anywhere else under ``ark/``.
"""

from __future__ import annotations

from typing import Optional

from .base import ControlPlaneClient
from .local_db import (
    LocalDbControlPlaneClient,
    default_db_path,
    resolve_project_id_by_name,
)
from .null import NullControlPlaneClient
from .types import Command, DecisionView, ProjectView

__all__ = [
    "ControlPlaneClient",
    "LocalDbControlPlaneClient",
    "NullControlPlaneClient",
    "Command",
    "DecisionView",
    "ProjectView",
    "build_client",
    "default_db_path",
    "resolve_project_id_by_name",
]


def build_client(*, control_plane_url: Optional[str] = None,
                 token: Optional[str] = None,
                 db_path: Optional[str] = None,
                 project_id: Optional[str] = None,
                 log_fn=None) -> ControlPlaneClient:
    """Select the right client for how the orchestrator was launched.

    * ``control_plane_url`` present → HTTP client (Phase 1, step 4 — not yet
      implemented; raises so misconfiguration is loud rather than silently Null).
    * else a usable ``db_path`` + ``project_id`` → in-process LocalDb client
      (auto-discovers the db path when not supplied).
    * else → Null client (Telegram-only / headless).
    """
    if control_plane_url:
        raise NotImplementedError(
            "HttpControlPlaneClient is not implemented yet (Phase 1, step 4). "
            "Launch with --db-path for the LocalDb path in the meantime."
        )

    if not db_path:
        db_path = default_db_path()

    if db_path and project_id:
        return LocalDbControlPlaneClient(db_path=db_path, project_id=project_id,
                                         log_fn=log_fn)

    return NullControlPlaneClient()
