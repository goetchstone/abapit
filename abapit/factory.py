"""Pick the right API client for an org's provider.

Apple and Mosyle clients share a duck-typed interface (the methods the
device pages call), so callers — the web app, the CLI — get a client back
and never branch on provider themselves.
"""

from __future__ import annotations

from .config import Org


def build_client(org: Org):
    if org.provider == "mosyle":
        from .mosyle import MosyleClient
        return MosyleClient(org)
    from .client import ApiClient
    return ApiClient(org)
