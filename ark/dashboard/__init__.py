"""ARK Dashboard — real-time web UI for monitoring research projects."""


def create_app():
    """Lazy import to avoid breaking 'ark' when fastapi is not installed."""
    from .app import create_app as _create
    return _create()


__all__ = ["create_app"]
