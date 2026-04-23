"""ARK Web App — lab-facing project submission & monitoring portal."""

def create_app(*args, **kwargs):
    """Lazy factory to avoid loading routes/authlib when only .db is needed."""
    from .app import create_app as _create_app
    return _create_app(*args, **kwargs)

__all__ = ["create_app"]
