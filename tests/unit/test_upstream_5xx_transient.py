"""An upstream provider 5xx must be retried, not treated as terminal.

A run that had already produced a clean 7.6/10 paper was discarded because
OpenRouter relayed "Upstream error from Nvidia: Internal server error" during
the review step. The error CODE was a generic APIError, so the code-based
transient list missed it — only the detail text names the real cause.
"""
import re
from pathlib import Path

SRC = Path("/home/xinj/ARK/ark/engines/__init__.py").read_text()


def _transient_details():
    m = re.search(r"_TRANSIENT_DETAIL = \((.*?)\n\s*\)", SRC, re.S)
    assert m, "could not locate _TRANSIENT_DETAIL"
    return [s.lower() for s in re.findall(r'"([^"]+)"', m.group(1))]


def _is_transient(detail: str) -> bool:
    d = (detail or "").lower()
    return any(sig in d for sig in _transient_details())


def test_the_exact_error_that_killed_the_run():
    assert _is_transient(
        "litellm.APIError: APIError: OpenrouterException - "
        "Upstream error from Nvidia: Internal server error")


def test_other_upstream_5xx_shapes():
    for d in ("Provider returned 503 Service Unavailable",
              "The model is temporarily unavailable, try again",
              "Upstream error from Anthropic: overloaded_error"):
        assert _is_transient(d), d


def test_real_terminal_errors_are_not_swallowed():
    """A retry loop that eats auth and quota failures is worse than the bug."""
    for d in ("Invalid API key provided",
              "This request requires more credits, or fewer max_tokens",
              "messages.32.content: The image was specified using the image/png "
              "media type, but the image appears to be a image/jpeg image",
              "model `foo/bar` not found"):
        assert not _is_transient(d), d
