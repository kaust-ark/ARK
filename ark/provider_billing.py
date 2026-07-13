"""Provider-billed cost truth.

Our per-call ledger prices token counts with litellm's static table — an
ESTIMATE that drifts from the provider's invoice (cache-discount assumptions,
platform fees, calls whose usage the API never returned). OpenRouter exposes
the authoritative number: ``GET /api/v1/key`` returns the calling key's
accumulated usage in USD. Sampling it at run start and at every ledger write
yields ``provider_billed_usd`` — every cent the run actually charged,
including spend our per-call ledger can't see (deep research, figure
generation, utility calls).

Attribution note: the delta covers everything billed to the key during the
run. Regular users can't run concurrent projects (per-user lane cap = 1), so
for them the delta IS the project's cost; concurrent admin runs may
over-attribute and the dashboard labels the figure accordingly.

Fail-open everywhere: no key / timeout / schema drift → None, callers keep
the estimate silently.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional


def openrouter_key_usage_usd(api_key: Optional[str] = None,
                             timeout: float = 5.0) -> Optional[float]:
    """The calling key's lifetime billed usage in USD, or None on any failure."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = (json.load(r) or {}).get("data") or {}
        usage = data.get("usage")
        return float(usage) if usage is not None else None
    except Exception:
        return None
